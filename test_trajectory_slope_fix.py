"""
Tests unitarios para el fix de auditoria (07/09/2026): _expected_offset_from_high
y _hour_sigma_multiplier ahora usan la PENDIENTE reciente real de la
trayectoria (_recent_trajectory_slope_f_per_hr) en vez de asumir "ya casi
llego al techo" solo por la hora del reloj en la ventana 15-18h.

No requiere red -- todo opera sobre series sinteticas / horas fijas, para
poder correrlo tal cual desde github.dev (Actions) o localmente con
`python3 test_trajectory_slope_fix.py`. Sale con status 0 si todo pasa,
imprime cada assert y termina con status 1 en el primer fallo.
"""
import sys

from weather_signal_engine import (
    _expected_offset_from_high,
    _hour_sigma_multiplier,
    _recent_trajectory_slope_f_per_hr,
)

passed = 0
failed = 0


def check(label, condition):
    global passed, failed
    if condition:
        print(f"OK   {label}")
        passed += 1
    else:
        print(f"FAIL {label}")
        failed += 1


# ---------------------------------------------------------------------------
# 1) _recent_trajectory_slope_f_per_hr -- calculo puro de pendiente
# ---------------------------------------------------------------------------

# Serie subiendo ~1.5F en la ultima hora -> pendiente ~+1.5F/hr
series_rising = [
    ("2026-09-07T18:00:00+00:00", 90.0),
    ("2026-09-07T19:00:00+00:00", 91.5),
]
slope = _recent_trajectory_slope_f_per_hr(series_rising)
check(f"pendiente subiendo ~1.5F/hr detectada (got {slope:.2f})", slope is not None and 1.3 <= slope <= 1.7)

# Serie plana -> pendiente ~0
series_flat = [
    ("2026-09-07T19:00:00+00:00", 92.0),
    ("2026-09-07T20:00:00+00:00", 92.1),
]
slope_flat = _recent_trajectory_slope_f_per_hr(series_flat)
check(f"pendiente plana detectada (got {slope_flat:.2f})", slope_flat is not None and abs(slope_flat) <= 0.15)

# Menos de 2 puntos -> None
check("menos de 2 puntos -> None", _recent_trajectory_slope_f_per_hr([("2026-09-07T19:00:00+00:00", 92.0)]) is None)
check("serie vacia -> None", _recent_trajectory_slope_f_per_hr([]) is None)

# Puntos demasiado cerca entre si (ruido de SPECI) -- debe ignorar el punto
# de 5 minutos y usar el de ~1h atras si esta disponible
series_noisy = [
    ("2026-09-07T18:00:00+00:00", 89.0),
    ("2026-09-07T18:55:00+00:00", 90.9),  # 5 min antes del ultimo -- se descarta por min_gap
    ("2026-09-07T19:00:00+00:00", 91.0),
]
slope_noisy = _recent_trajectory_slope_f_per_hr(series_noisy)
check(
    f"ignora observacion a 5 min y usa la de ~1h (got {slope_noisy:.2f})",
    slope_noisy is not None and 1.8 <= slope_noisy <= 2.2,
)

# Gap demasiado grande (>150 min) -- no hay con que comparar de forma
# confiable, debe devolver None
series_gap = [
    ("2026-09-07T15:00:00+00:00", 88.0),
    ("2026-09-07T19:00:00+00:00", 91.0),
]
check("gap > 150 min -> None (no hay referencia confiable)", _recent_trajectory_slope_f_per_hr(series_gap) is None)


# ---------------------------------------------------------------------------
# 2) _expected_offset_from_high -- comportamiento previo intacto sin slope
# ---------------------------------------------------------------------------
check("hour<9 -> None (sin cambios)", _expected_offset_from_high(6) is None)
check("hour>18 -> None (sin cambios)", _expected_offset_from_high(20) is None)
check("9<=hour<12 -> 6.0 sin slope (sin cambios)", _expected_offset_from_high(10) == 6.0)
check("12<=hour<15 -> 3.0 sin slope (sin cambios)", _expected_offset_from_high(13) == 3.0)
check("15<=hour<=18 -> 0.5 sin slope (sin cambios, compat hacia atras)", _expected_offset_from_high(16) == 0.5)

# ---------------------------------------------------------------------------
# 3) El caso que motivo el fix: 16h, offset colapsaba a 0.5 SIEMPRE.
#    Con slope fuerte (sigue subiendo), debe ensancharse de nuevo.
# ---------------------------------------------------------------------------
offset_still_rising = _expected_offset_from_high(16, slope=1.2)  # sigue subiendo 1.2F/hr
check(
    f"16h + sigue subiendo fuerte (1.2F/hr) -> offset ensanchado, no 0.5 (got {offset_still_rising})",
    offset_still_rising >= 2.0,
)

offset_flat_at_16 = _expected_offset_from_high(16, slope=0.05)
check(
    f"16h + trayectoria realmente plana -> offset angosto (got {offset_flat_at_16})",
    offset_flat_at_16 <= 0.5,
)

# Trayectoria se aplana TEMPRANO (13h) -- ahora puede angostarse antes de
# que el reloj diga 15h, en vez de esperar a la hora fija.
offset_early_flat = _expected_offset_from_high(13, slope=0.05)
check(
    f"13h + trayectoria ya plana -> offset mas angosto que el baseline de 3.0 (got {offset_early_flat})",
    offset_early_flat < 3.0,
)

# Sin slope disponible (fuente cayo) -- debe caer al comportamiento previo
check(
    "16h sin slope disponible -> cae al comportamiento previo (0.5)",
    _expected_offset_from_high(16, slope=None) == 0.5,
)


# ---------------------------------------------------------------------------
# 4) _hour_sigma_multiplier -- mismo patron
# ---------------------------------------------------------------------------
check("hour None -> 1.6 (sin cambios)", _hour_sigma_multiplier(None) == 1.6)
check("16h sin slope -> 0.85 (compat hacia atras)", _hour_sigma_multiplier(16) == 0.85)

mult_still_rising = _hour_sigma_multiplier(16, slope=1.2)
check(
    f"16h + sigue subiendo fuerte -> sigma NO se angosta a 0.85 (got {mult_still_rising})",
    mult_still_rising >= 1.35,
)

mult_flat = _hour_sigma_multiplier(16, slope=0.05)
check(
    f"16h + trayectoria realmente plana -> sigma se mantiene angosto (got {mult_flat})",
    mult_flat <= 0.95,
)


# ---------------------------------------------------------------------------
# 5) Caso end-to-end sintetico, replicando el patron real de la auditoria:
#    señal a las 16h, centro NWS = 90F, pero la trayectoria de la ultima
#    hora subio a 91.5F y sigue subiendo 1.5F/hr -- con el codigo viejo el
#    modelo habria comprado el bucket "de turno" cerca de la lectura
#    actual; con el fix, offset y sigma deben reflejar que todavia falta
#    camino por recorrer.
# ---------------------------------------------------------------------------
hour = 16
slope_case = _recent_trajectory_slope_f_per_hr(
    [("2026-09-07T19:00:00+00:00", 90.0), ("2026-09-07T20:00:00+00:00", 91.5)]
)
offset_case = _expected_offset_from_high(hour, slope=slope_case)
sigma_mult_case = _hour_sigma_multiplier(hour, slope=slope_case)
check(
    f"caso real (16h, trayectoria subiendo 1.5F/hr): offset >= 3.0 (got {offset_case})",
    offset_case >= 3.0,
)
check(
    f"caso real (16h, trayectoria subiendo 1.5F/hr): sigma_mult >= 1.35, no 0.85 (got {sigma_mult_case})",
    sigma_mult_case >= 1.35,
)


print()
print(f"{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
