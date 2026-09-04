"""
Motor de análisis de clima para mercados de Polymarket.

Implementa la metodología STEP 0-3 de la skill `wu-airport-weather`
(resolución de estación de asentamiento, distribución de probabilidad por
bucket de temperatura, comparación contra precio de mercado y EV) usando
ÚNICAMENTE fuentes con API oficial, gratuita y estable:

  - NWS (api.weather.gov)          → guía de pronóstico oficial
  - aviationweather.gov            → METAR (observación en vivo + máxima
                                      de 6h en remarks) y TAF (timing de
                                      tormenta/nubes)

Deliberadamente NO incluye Weather Underground ni AccuWeather como fuente
automatizada acá: WU no tiene API pública gratuita confiable (solo scraping
frágil, mal candidato para un cron desatendido) y AccuWeather requiere API
key paga. Para el cruce multi-fuente completo que describe la skill
original, usar `weather_report.py` (modo manual) o pedirle directamente a
Claude que corra la skill `wu-airport-weather` en una conversación — ahí sí
tiene sentido porque hay un humano revisando cada corrida.

Este módulo es puro (sin I/O de Telegram/DB) salvo `WeatherNotifyStateStore`,
que sí necesita persistencia mínima para deduplicar avisos entre ciclos.
Los orquestadores (api/weather_cycle.py para serverless, weather_report.py
para modo manual) importan de acá.
"""
import logging
import math
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger("weather_signal_engine")

NWS_API = "https://api.weather.gov"
AWC_API = "https://aviationweather.gov/api/data"

DEFAULT_TIMEOUT = 6


def _capped_timeout(time_left_fn, ceiling=DEFAULT_TIMEOUT, floor=1.0, safety_margin=0.5):
    """Cap del timeout de UNA request según lo que quede del budget global
    del ciclo, en vez de un timeout fijo ciego a cuánto ya se gastó.

    FIX (02/09/2026): fetch_nws_guidance (2 requests secuenciales) + METAR +
    TAF, cada una con DEFAULT_TIMEOUT=6s fijo, podían sumar hasta ~24s en el
    peor caso -- casi todo el maxDuration=25 de Vercel -- para UN solo
    evento. El time_left() del loop en run_weather_cycle() (app.py) solo
    corta ENTRE eventos, no puede interrumpir requests ya en curso dentro de
    uno. Resultado: 504 FUNCTION_INVOCATION_TIMEOUT intermitente, solo en
    los ciclos donde NWS/METAR/TAF andaban lentos cerca del máximo -- de ahí
    que "a veces sí, a veces no". Con esto cada request usa como timeout lo
    que quede del budget (con margen), así que en el peor caso el propio
    requests.get() corta antes de que lo mate Vercel, y el ciclo devuelve
    una respuesta parcial en vez de un 504 que pierde TODO el trabajo del
    ciclo (incluidas las estaciones ya procesadas antes)."""
    if time_left_fn is None:
        return ceiling
    remaining = time_left_fn() - safety_margin
    return max(floor, min(ceiling, remaining))

# ---------------------------------------------------------------------------
# STEP 0 — Resolución de estación de asentamiento (NUNCA asumir)
# ---------------------------------------------------------------------------
# Punto de partida para las ciudades que más aparecen en mercados de clima
# de Polymarket. `verified=False` marca los casos donde el mapeo ciudad→ICAO
# es ambiguo o no es la estación oficial de asentamiento — la skill original
# es explícita en que equivocarse acá es la forma #1 de perder con un
# pronóstico correcto. Revisar `note` y las reglas de resolución del mercado
# puntual antes de operar con esos casos.
STATION_MAP = {
    "miami": {
        "icao": "KMIA", "lat": 25.7617, "lon": -80.1918,
        "tz": "America/New_York", "name": "Miami Intl (KMIA)",
        "verified": True,
    },
    "new york": {
        "icao": "KLGA", "lat": 40.7769, "lon": -73.8740,
        "tz": "America/New_York", "name": "LaGuardia (KLGA) — proxy de Central Park",
        "verified": False,
        "note": ("Muchos mercados de 'NYC' asientan sobre el CLI report de "
                 "Central Park, que no tiene METAR/TAF propio en vivo. Se usa "
                 "KLGA como proxy de observación — CONFIRMAR la estación real "
                 "en las reglas del mercado antes de operar; puede diferir "
                 "varios grados de Central Park algunos días."),
    },
    "nyc": {  # alias
        "icao": "KLGA", "lat": 40.7769, "lon": -73.8740,
        "tz": "America/New_York", "name": "LaGuardia (KLGA) — proxy de Central Park",
        "verified": False,
        "note": "Ver nota de 'new york'.",
    },
    "chicago": {
        "icao": "KMDW", "lat": 41.7868, "lon": -87.7522,
        "tz": "America/Chicago", "name": "Midway (KMDW)", "verified": True,
    },
    "los angeles": {
        "icao": "KLAX", "lat": 33.9425, "lon": -118.4081,
        "tz": "America/Los_Angeles", "name": "LAX (KLAX)", "verified": True,
    },
    "philadelphia": {
        "icao": "KPHL", "lat": 39.8721, "lon": -75.2411,
        "tz": "America/New_York", "name": "Philadelphia Intl (KPHL)", "verified": True,
    },
    "austin": {
        "icao": "KAUS", "lat": 30.1975, "lon": -97.6664,
        "tz": "America/Chicago", "name": "Austin-Bergstrom (KAUS)", "verified": True,
    },
    "denver": {
        "icao": "KDEN", "lat": 39.8617, "lon": -104.6731,
        "tz": "America/Denver", "name": "Denver Intl (KDEN)", "verified": True,
    },
    "houston": {
        "icao": "KHOU", "lat": 29.6454, "lon": -95.2789,
        "tz": "America/Chicago", "name": "Houston Hobby (KHOU)", "verified": True,
    },
    "phoenix": {
        "icao": "KPHX", "lat": 33.4342, "lon": -112.0116,
        "tz": "America/Phoenix", "name": "Phoenix Sky Harbor (KPHX)", "verified": True,
    },
    "dallas": {
        "icao": "KDFW", "lat": 32.8998, "lon": -97.0403,
        "tz": "America/Chicago", "name": "DFW (KDFW)", "verified": True,
    },
    "boston": {
        "icao": "KBOS", "lat": 42.3656, "lon": -71.0096,
        "tz": "America/New_York", "name": "Logan (KBOS)", "verified": True,
    },
    "seattle": {
        "icao": "KSEA", "lat": 47.4502, "lon": -122.3088,
        "tz": "America/Los_Angeles", "name": "Sea-Tac (KSEA)", "verified": True,
    },
    "atlanta": {
        "icao": "KATL", "lat": 33.6407, "lon": -84.4277,
        "tz": "America/New_York", "name": "Hartsfield-Jackson (KATL)", "verified": True,
    },
}


def resolve_station(text, override_icao=None):
    """Resuelve la estación de asentamiento a partir del título del evento
    o de un ICAO forzado manualmente. Devuelve None si no matchea nada —
    generate_weather_signal trata eso como 'no operable', nunca adivina."""
    if override_icao:
        return {
            "icao": override_icao, "lat": None, "lon": None, "tz": "UTC",
            "name": override_icao, "verified": False,
            "note": "Estación forzada manualmente vía WEATHER_STATION_OVERRIDE — sin geocodificar, el ajuste por trayectoria matutina usa hora UTC.",
        }
    q = (text or "").lower()
    for city, info in STATION_MAP.items():
        if city in q:
            return info
    return None


# ---------------------------------------------------------------------------
# Parseo del bucket de temperatura desde el texto del sub-mercado
# ---------------------------------------------------------------------------
_BUCKET_PATTERNS = [
    (re.compile(r"(\d{1,3})\s*°?F?\s*(?:or (?:above|higher|more)|\+\b)", re.I), "tail_high"),
    (re.compile(r"(\d{1,3})\s*°?F?\s*(?:or (?:below|lower|less))", re.I), "tail_low"),
    (re.compile(r"(\d{1,3})\s*°?F?\s*(?:-|–|to)\s*(\d{1,3})\s*°?F?", re.I), "range"),
    (re.compile(r"\b(\d{1,3})\s*°\s*F?\b"), "single"),
    (re.compile(r"\b(\d{1,3})\s*degrees?\b", re.I), "single"),
]


def parse_bucket(question):
    """Extrae el rango numérico (°F) de la pregunta de un sub-mercado de
    bucket. Best-effort: las preguntas de Polymarket no tienen un formato
    100% estable entre eventos, así que esto puede fallar en frases nuevas
    — devuelve None en ese caso y ese mercado se excluye de la distribución
    en vez de arriesgar ubicarlo en el bucket equivocado."""
    if not question:
        return None
    for pattern, kind in _BUCKET_PATTERNS:
        m = pattern.search(question)
        if not m:
            continue
        if kind == "range":
            lo, hi = int(m.group(1)), int(m.group(2))
            return {"kind": "range", "low": min(lo, hi), "high": max(lo, hi)}
        val = int(m.group(1))
        if kind == "tail_high":
            return {"kind": "tail_high", "low": val, "high": None}
        if kind == "tail_low":
            return {"kind": "tail_low", "low": None, "high": val}
        return {"kind": "single", "low": val, "high": val}
    return None


# ---------------------------------------------------------------------------
# STEP 1 — Recolección de datos (fuentes oficiales)
# ---------------------------------------------------------------------------
def _headers(config):
    ua = getattr(config, "WEATHER_USER_AGENT", None) or "lstrade-weather-bot/1.0"
    # Los headers HTTP viajan en latin-1 — un guión largo "—", comillas
    # tipográficas “ ”, o una tilde mal copiada al pegar el contacto en la
    # env var de Vercel hacen que `requests` lance UnicodeEncodeError ANTES
    # de intentar la conexión (ni DNS llega a resolver). Eso lo atrapaba el
    # except Exception de cada fetch_* de abajo y se veía como "no_data" sin
    # ningún rastro del motivo real — pasaba en los 3 fetches, en cada
    # evento, por eso weather_signals nunca se pobló pese a que el cronjob
    # corría bien (confirmado: la ejecución tardaba ~1.5s, muy poco para
    # llegar a hacer las llamadas reales a NWS/aviationweather.gov). Se sanea
    # acá para que un typo al configurar el contacto no tumbe el módulo
    # entero — si hay caracteres no soportados se descartan y se loguea un
    # aviso, en vez de fallar en silencio.
    #
    # Segundo bug (01/09/2026): la env var WEATHER_USER_AGENT en Vercel quedó
    # cargada con espacios en blanco al inicio (probablemente por un
    # copy-paste con indentación). Los headers HTTP no toleran whitespace
    # inicial/final ni CR/LF embebidos — el cliente HTTP lo rechaza con
    # ValueError ANTES de la conexión ("Invalid leading whitespace, reserved
    # character(s), or return character(s) in header value"), que tampoco
    # es un UnicodeEncodeError, así que el chequeo de arriba no lo agarraba.
    # Se sanea acá con strip() + remoción de CR/LF, sea cual sea la causa.
    ua = re.sub(r"[\r\n]", "", ua).strip() or "lstrade-weather-bot/1.0"
    try:
        ua.encode("latin-1")
    except UnicodeEncodeError:
        sanitized = ua.encode("ascii", "ignore").decode("ascii").strip() or "lstrade-weather-bot/1.0"
        log.warning(
            f"WEATHER_USER_AGENT tiene caracteres no soportados en headers HTTP "
            f"(ej. guión largo, comillas curvas) — usando versión saneada: {sanitized!r}"
        )
        ua = sanitized
    return {"User-Agent": ua, "Accept": "application/json"}


def _target_local_date(event, station):
    """Fecha (en huso local de la estación) que realmente liquida el
    evento, a partir del `end_date` (endDate de Polymarket) que ya viene en
    cada bucket parseado.

    AUDITORÍA (03/09/2026): esta función no existía. fetch_nws_guidance
    tomaba ciegamente el primer período "isDaytime" de la grilla de NWS
    (services.periods[0] o el próximo daytime), asumiendo que siempre era
    el día que liquida el mercado. Si algún evento del batch resolvía
    "mañana" en vez de "hoy" -- Polymarket publica ambos con frecuencia
    para la misma ciudad -- el motor comparaba el pronóstico del día
    equivocado contra ese mercado sin ninguna señal de alerta. Devuelve
    None si no hay end_date parseable; en ese caso el llamador debe
    degradar confianza en vez de asumir "hoy".
    """
    end_date = None
    for m in event.get("markets", []):
        end_date = m.get("end_date")
        if end_date:
            break
    if not end_date:
        return None
    try:
        dt = datetime.fromisoformat(str(end_date).replace("Z", "+00:00"))
        return dt.astimezone(ZoneInfo(station["tz"])).date()
    except Exception:
        return None


def fetch_nws_guidance(station, config, timeout=DEFAULT_TIMEOUT, time_left_fn=None, target_date=None):
    """Guía de pronóstico oficial de NWS para el punto de la estación.
    api.weather.gov exige un User-Agent identificable (no un navegador
    genérico) — configurar WEATHER_USER_AGENT con un contacto real o NWS
    puede empezar a bloquear las requests.

    `target_date`: date en huso local de la estación que realmente liquida
    el mercado (ver _target_local_date). Si se pasa, se busca el período
    diurno de la grilla de NWS cuyo startTime cae en esa fecha, en vez de
    asumir que el primer período diurno de la respuesta es el correcto
    (AUDITORÍA 03/09/2026 — ver _target_local_date)."""
    if station.get("lat") is None:
        return None
    try:
        headers = _headers(config)
        t1 = _capped_timeout(time_left_fn, ceiling=timeout)
        points = requests.get(
            f"{NWS_API}/points/{station['lat']},{station['lon']}",
            headers=headers, timeout=t1,
        ).json()
        forecast_url = points["properties"]["forecast"]
        # FIX (02/09/2026): la llamada de arriba ya pudo haber consumido
        # varios segundos del budget -- se recalcula el timeout para esta
        # segunda request en vez de reusar el mismo `timeout` fijo, que
        # antes dejaba que las 2 requests de este método solas se comieran
        # hasta 2x DEFAULT_TIMEOUT.
        t2 = _capped_timeout(time_left_fn, ceiling=timeout)
        forecast = requests.get(forecast_url, headers=headers, timeout=t2).json()
        periods = forecast["properties"]["periods"]
        date_mismatch = False
        chosen = None
        if target_date is not None:
            for p in periods:
                if not p.get("isDaytime"):
                    continue
                try:
                    p_date = datetime.fromisoformat(p["startTime"]).astimezone(
                        ZoneInfo(station["tz"])
                    ).date()
                except Exception:
                    continue
                if p_date == target_date:
                    chosen = p
                    break
            if chosen is None:
                # No hay período diurno para esa fecha en la grilla (fuera
                # de rango, o el parseo de end_date falló) -- se degrada en
                # vez de adivinar con el primer período disponible.
                date_mismatch = True
        if chosen is None:
            chosen = next((p for p in periods if p.get("isDaytime")), periods[0])
        return {
            "forecast_high_f": chosen.get("temperature"),
            "short_forecast": chosen.get("shortForecast"),
            "issued": forecast["properties"].get("updated"),
            "date_mismatch": date_mismatch,
        }
    except Exception as e:
        log.warning(f"NWS guidance falló para {station.get('icao')}: {e}")
        return None


def _parse_six_hour_max_f(raw_ob):
    """Grupo de remarks METAR '1sTTT' = máxima de 6h (s: 0=+, 1=-; TTT en
    décimas de °C) — captura picos entre observaciones horarias que la
    lectura puntual se puede perder. Ej: '10061' = +6.1°C."""
    if not raw_ob:
        return None
    m = re.search(r"\b1(\d)(\d{3})\b", raw_ob)
    if not m:
        return None
    sign = -1 if m.group(1) == "1" else 1
    celsius = sign * int(m.group(2)) / 10
    return celsius * 9 / 5 + 32


def fetch_metar(icao, config, hours=3, timeout=DEFAULT_TIMEOUT, time_left_fn=None):
    """Observación en vivo + máxima de 6h de los remarks, tal como pide
    STEP 1 de la skill (watch the 6-hour max temperature group)."""
    try:
        headers = _headers(config)
        resp = requests.get(
            f"{AWC_API}/metar",
            params={"ids": icao, "format": "json", "hours": hours},
            headers=headers, timeout=_capped_timeout(time_left_fn, ceiling=timeout),
        )
        resp.raise_for_status()
        obs = resp.json()
        if not obs:
            return None
        latest = obs[0]
        temp_c = latest.get("temp")
        return {
            "temp_f": (temp_c * 9 / 5 + 32) if temp_c is not None else None,
            "obs_time": latest.get("obsTime") or latest.get("reportTime"),
            "raw": latest.get("rawOb"),
            "six_hr_max_f": _parse_six_hour_max_f(latest.get("rawOb", "")),
        }
    except Exception as e:
        log.warning(f"METAR falló para {icao}: {e}")
        return None


def fetch_taf(icao, config, timeout=DEFAULT_TIMEOUT, time_left_fn=None):
    """TAF para timing de nubes/tormenta — lo que realmente topea la
    máxima del día (STEP 1/2 de la skill)."""
    try:
        headers = _headers(config)
        resp = requests.get(
            f"{AWC_API}/taf",
            params={"ids": icao, "format": "json"},
            headers=headers, timeout=_capped_timeout(time_left_fn, ceiling=timeout),
        )
        resp.raise_for_status()
        data = resp.json()
        if not data:
            return None
        taf = data[0]
        raw = taf.get("rawTAF") or taf.get("raw_text") or ""
        storm_signal = bool(re.search(r"\b(TS|SH|VCTS|VCSH)\w*", raw))
        return {"raw": raw, "issue_time": taf.get("issueTime"), "storm_signal": storm_signal}
    except Exception as e:
        log.warning(f"TAF falló para {icao}: {e}")
        return None


# ---------------------------------------------------------------------------
# STEP 2 — Estimación de la máxima ajustada por física del día
# ---------------------------------------------------------------------------
def _station_local_hour(station):
    try:
        return datetime.now(ZoneInfo(station["tz"])).hour
    except Exception:
        return 12


def _expected_offset_from_high(hour):
    """Cuántos °F suelen faltar para la máxima del día, según la hora local.

    Hallazgo 01/09/2026: la versión anterior usaba un offset fijo (-6°F) y
    solo comparaba la trayectoria real (METAR) contra el pronóstico entre
    las 9am y las 2pm. Eso deja sin chequear justo la ventana de pico de
    calor real (~2-5pm en la mayoría de estaciones) — si la tarde ya
    contradice el pronóstico matutino de NWS, el modelo dejaba de
    enterarse pasadas las 2pm, mientras que el precio de mercado sí lo
    refleja en tiempo real (así se explicó el desacuerdo grande en la
    señal de KDFW: 35% del modelo vs. <1% del mercado, generada a las
    ~2:30pm hora local). La curva diurna real sube rápido a media mañana y
    se aplana cerca del pico, así que un offset fijo tampoco sería correcto
    al extender la ventana sin más — cerca del pico ya debería faltar poco.
    """
    if hour < 9:
        return None  # muy temprano — la trayectoria matutina todavía no dice nada útil
    if hour < 12:
        return 6.0   # media mañana — normalmente faltan ~5-8°F para la máxima
    if hour < 15:
        return 3.0   # primera hora de la tarde — se va cerrando la brecha
    if hour <= 18:
        return 0.5   # ventana de pico / post-pico — la máxima ya debería estar casi alcanzada
    return None      # noche — la trayectoria del día ya no es informativa


def estimate_adjusted_high(nws, metar, taf, station):
    """Parte de la guía de NWS como centro de masa y ajusta con la
    trayectoria matutina (METAR vs. lo esperado a esta hora) y el timing de
    tormenta (TAF) — mismo criterio que STEP 2 de la skill. Devuelve
    (estimación_f, notas[], penalización_de_confianza)."""
    notes = []
    penalty = 0.0
    base = nws["forecast_high_f"] if nws and nws.get("forecast_high_f") is not None else None

    if base is None and metar and metar.get("temp_f") is not None:
        base = metar["temp_f"] + 5
        notes.append("Sin guía NWS disponible — estimación gruesa desde METAR actual +5°F.")
        penalty += 0.3

    if base is None:
        return None, ["Sin ninguna fuente de guía disponible."], 1.0

    adjustment = 0.0
    hour = _station_local_hour(station)

    offset = _expected_offset_from_high(hour)
    if metar and metar.get("temp_f") is not None and offset is not None:
        current = metar["temp_f"]
        six_hr_max = metar.get("six_hr_max_f")
        reference = max(current, six_hr_max) if six_hr_max else current
        expected_at_this_hour = base - offset
        delta = reference - expected_at_this_hour
        if abs(delta) >= 2:
            adjustment += max(-3.0, min(3.0, delta * 0.4))
            notes.append(f"Trayectoria del día {delta:+.1f}°F vs. lo esperado a las {hour}h — ajuste {adjustment:+.1f}°F.")

    if taf and taf.get("storm_signal") and hour < 16:
        adjustment -= 1.5
        notes.append("TAF muestra tormenta/chubascos antes de cerrar la ventana de pico de calor (1-4pm) — techo a la baja (-1.5°F).")

    if not notes:
        notes.append("Sin ajustes intradía significativos — se mantiene la guía de NWS.")

    if nws is None:
        penalty += 0.15
    if metar is None:
        penalty += 0.15
        notes.append("Sin observación METAR — no se pudo chequear trayectoria matutina, se ensancha la distribución.")
    if taf is None:
        penalty += 0.1

    return round(base + adjustment, 1), notes, round(min(penalty, 1.0), 2)


# ---------------------------------------------------------------------------
# STEP 2.3 — Distribución de probabilidad por bucket (nunca sobre-concentrar)
# ---------------------------------------------------------------------------
def _norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _bucket_probability(center, sigma, bucket):
    if bucket["kind"] == "tail_high":
        lo = bucket["low"] - 0.5
        return 1 - _norm_cdf((lo - center) / sigma)
    if bucket["kind"] == "tail_low":
        hi = bucket["high"] + 0.5
        return _norm_cdf((hi - center) / sigma)
    lo = bucket["low"] - 0.5
    hi = bucket["high"] + 0.5
    return max(0.0, _norm_cdf((hi - center) / sigma) - _norm_cdf((lo - center) / sigma))


def build_bucket_distribution(center, buckets, base_sigma=1.6, confidence_penalty=0.0):
    """Reparte la masa de probabilidad entre buckets adyacentes con una
    normal continua integrada por bucket, en vez de concentrarla en uno
    solo — la skill marca esto explícitamente como el error más común
    ('overconfident single-bucket distributions'). `confidence_penalty`
    (0-1, viene de estimate_adjusted_high) ensancha sigma cuando faltó
    alguna fuente de datos."""
    sigma = base_sigma * (1 + confidence_penalty)
    raw = {}
    for b in buckets:
        raw[b["condition_id"]] = _bucket_probability(center, sigma, b["parsed_bucket"])
    total = sum(raw.values()) or 1.0
    return {cid: p / total for cid, p in raw.items()}, sigma


# ---------------------------------------------------------------------------
# STEP 3 — Comparación contra mercado y EV
# ---------------------------------------------------------------------------
def compute_ev(prob, price):
    if not price or price <= 0:
        return None
    return (prob / price) - 1


DISCLAIMER = (
    "No es asesoría financiera. Los contratos de mercados de predicción son "
    "todo-o-nada; se puede perder el monto arriesgado al liquidar. Estimación "
    "educativa a partir de fuentes meteorológicas oficiales (NWS, METAR, TAF) "
    "— no incluye Weather Underground/AccuWeather (ver weather_report.py para "
    "el cruce manual con esas fuentes)."
)


def generate_weather_signal(event, config, min_ev=0.15, min_price=0.01, time_left_fn=None, client=None):
    """
    Genera una señal de clima para un evento de Polymarket agrupado por
    buckets (mercados YES/NO por rango de temperatura del mismo día/estación).

    `event`: {"title": str, "markets": [market_parseado, ...]} — cada market
    viene de PolymarketClient.parse_market_for_analysis (o compatible).

    `time_left_fn`: función opcional que devuelve segundos restantes del
    budget global del ciclo (ver run_weather_cycle en app.py). Si se pasa,
    los timeouts de NWS/METAR/TAF se ajustan dinámicamente a lo que quede
    (FIX 02/09/2026, ver _capped_timeout) en vez de usar DEFAULT_TIMEOUT
    fijo, que en el peor caso podía comerse ~24s solo para las 3 fuentes de
    UN evento y provocar un 504 intermitente en Vercel.

    `client`: instancia de PolymarketClient, usada para verificar el
    candidato a best_trade contra el order book real (fetch_order_book_snapshot)
    antes de fijarlo — ver auditoría del 04/09/2026 más abajo. Si es None,
    no se puede verificar y el motor no elige best_trade (prefiere no
    operar a operar contra un precio de Gamma potencialmente viejo).

    Devuelve un dict con "status" en {"no_station", "no_buckets", "no_data",
    "sin_tiempo", "ok"} — nunca lanza excepción por datos faltantes, siempre
    degrada.
    """
    title = event.get("title") or ""
    station = resolve_station(title, override_icao=getattr(config, "WEATHER_STATION_OVERRIDE", None))
    if not station:
        return {"status": "no_station", "title": title}

    buckets = []
    for m in event.get("markets", []):
        parsed = parse_bucket(m.get("question"))
        if not parsed:
            continue
        m = dict(m)
        m["parsed_bucket"] = parsed
        buckets.append(m)

    if not buckets:
        return {"status": "no_buckets", "title": title, "station": station}

    if time_left_fn and time_left_fn() < 1.5:
        return {"status": "sin_tiempo", "title": title, "station": station}
    target_date = _target_local_date(event, station)
    nws = fetch_nws_guidance(station, config, time_left_fn=time_left_fn, target_date=target_date)

    if time_left_fn and time_left_fn() < 1.0:
        metar, taf = None, None
    else:
        metar = fetch_metar(station["icao"], config, time_left_fn=time_left_fn)
        if time_left_fn and time_left_fn() < 1.0:
            taf = None
        else:
            taf = fetch_taf(station["icao"], config, time_left_fn=time_left_fn)

    center, notes, penalty = estimate_adjusted_high(nws, metar, taf, station)
    if center is None:
        return {"status": "no_data", "title": title, "station": station}

    # AUDITORÍA (03/09/2026): si no se pudo confirmar que el período de NWS
    # usado corresponde al día real de liquidación del mercado (ver
    # _target_local_date / fetch_nws_guidance), no hay forma honesta de
    # confiar en el centro estimado -- se ensancha fuerte la distribución
    # en vez de operar con la misma confianza que un caso donde sí se
    # verificó la fecha.
    if nws and nws.get("date_mismatch"):
        penalty = min(1.0, penalty + 0.4)
        notes.append("No se pudo confirmar que el pronóstico de NWS usado corresponda al día que liquida este mercado -- confianza reducida.")

    base_sigma = getattr(config, "WEATHER_BASE_SIGMA_F", 2.4)
    distribution, sigma = build_bucket_distribution(center, buckets, base_sigma=base_sigma, confidence_penalty=penalty)

    # AUDITORÍA (03/09/2026): la skill de referencia (wu-airport-weather,
    # STEP 0) es explícita en que si la estación de asentamiento no está
    # confirmada hay que "tratar cualquier trade como especulativo". El
    # código sólo mostraba un emoji de advertencia en el mensaje de
    # Telegram (verified_flag en build_weather_memo) pero elegía best_trade
    # exactamente igual que con una estación verificada -- la advertencia
    # nunca cambiaba si el bot realmente operaba o no. Se exige el doble de
    # EV para calificar cuando la estación es un proxy sin confirmar (ej.
    # KLGA por NYC/Central Park), en vez de dejar que compita en igualdad
    # de condiciones con estaciones confirmadas.
    effective_min_ev = min_ev if station.get("verified", False) else max(min_ev * 2, min_ev + 0.15)
    min_liquidity = getattr(config, "WEATHER_MIN_LIQUIDITY", 200.0)
    max_ev = getattr(config, "WEATHER_MAX_EV", 3.0)

    rows = []
    for m in buckets:
        prob = distribution.get(m["condition_id"], 0.0)
        price = m.get("yes_price") or 0.0
        ev = compute_ev(prob, price)
        row = {
            "condition_id": m["condition_id"],
            "question": m["question"],
            "my_prob": prob,
            "market_price": price,
            "ev": ev,
            "liquidity": m.get("liquidity", 0),
            "yes_token_id": m.get("yes_token_id"),
            # AUDITORÍA (03/09/2026): antes esto priorizaba m.get("url"),
            # construido en _build_market_url() a partir del slug del
            # mercado individual (bucket). En clima cada evento tiene
            # varios buckets y su slug casi nunca coincide con el del
            # evento -- Polymarket sirve las páginas bajo /event/{event_slug},
            # no bajo el slug del mercado, así que ese link caía en 404. El
            # link del evento (event.get("url"), con el slug real del
            # evento) sí resuelve siempre, así que ahora es el que manda;
            # el slug del bucket queda solo como último recurso.
            "url": event.get("url") or m.get("url"),
        }
        rows.append(row)

    # min_price sigue descartando buckets pegados al piso de Polymarket como
    # candidatos: ahí el EV se dispara por dividir casi entre cero, no por
    # ventaja real. Igual quedan en `rows` para el detalle del reporte.
    rows.sort(key=lambda r: (r["ev"] if r["ev"] is not None else -999), reverse=True)

    # AUDITORÍA (04/09/2026, tras 20 señales cerradas y 15% de aciertos):
    # la versión anterior fijaba best_trade comparando mi_prob contra
    # `yes_price` de Gamma (outcomePrices) -- el ÚLTIMO PRECIO OPERADO, no
    # lo que cuesta comprar ahora. En un bucket barato e ilíquido (justo el
    # perfil que este motor busca: "el mercado lo cree improbable, yo no")
    # ese último trade puede tener horas y estar muy por debajo del ask
    # real -- el EV que se mandaba a Telegram no era ejecutable. Tampoco
    # había piso de liquidez: `liquidity` se calculaba por fila pero nunca
    # se usaba para descartar `best`. Con Brier 0.144 pero solo 15% de
    # aciertos, el modelo no estaba muy mal calibrado en promedio -- el
    # problema era la SELECCIÓN: maximizar EV=mi_prob/precio-1 sobre el
    # propio modelo, sin verificar contra el book real, concentra las
    # apuestas justo en los buckets donde un precio viejo o un error chico
    # de cola del modelo genera el EV más inflado (winner's curse).
    #
    # Fix: antes de fijar best_trade se re-verifica el candidato top (por
    # EV contra Gamma) contra el order book real -- se recalcula el EV con
    # el ask real, se exige WEATHER_MIN_LIQUIDITY y se descarta cualquier
    # EV verificado por encima de WEATHER_MAX_EV como más probable error de
    # modelo que ventaja real. Si el candidato top no pasa, se prueba el
    # siguiente por EV (hasta 3) antes de rendirse -- un candidato caro no
    # es autómaticamente malo, solo el que no resiste verificación real.
    best = None
    discard_notes = []
    candidates = [
        r for r in rows
        if r["ev"] is not None and r["ev"] >= effective_min_ev and r["market_price"] >= min_price
    ][:3]

    if client is None and candidates:
        discard_notes.append(
            "Sin cliente de Polymarket disponible para verificar el book real "
            "-- no se opera contra un precio de Gamma sin confirmar."
        )
    for cand in candidates if client is not None else []:
        label = cand["question"][:40]
        if time_left_fn and time_left_fn() < 1.2:
            discard_notes.append(f"{label}: sin tiempo para verificar book real.")
            break
        token_id = cand.get("yes_token_id")
        if not token_id:
            discard_notes.append(f"{label}: sin yes_token_id, no se puede verificar book.")
            continue
        snap = client.fetch_order_book_snapshot(
            token_id, timeout=_capped_timeout(time_left_fn, ceiling=DEFAULT_TIMEOUT)
        )
        if not snap or snap.get("best_ask") is None:
            discard_notes.append(f"{label}: book real no disponible.")
            continue
        real_liquidity = snap.get("liquidity") or 0.0
        if real_liquidity < min_liquidity:
            discard_notes.append(f"{label}: liquidez real ${real_liquidity:.0f} < piso ${min_liquidity:.0f}.")
            continue
        real_price = snap["best_ask"]
        real_ev = compute_ev(cand["my_prob"], real_price)
        if real_ev is None or real_ev < effective_min_ev:
            shown = f"{real_ev * 100:.0f}%" if real_ev is not None else "N/A"
            discard_notes.append(
                f"{label}: EV real {shown} < umbral con el ask real "
                f"(contra Gamma era {cand['ev'] * 100:.0f}%)."
            )
            continue
        if real_ev > max_ev:
            discard_notes.append(
                f"{label}: EV real {real_ev * 100:.0f}% por encima del techo de sanidad "
                f"({max_ev * 100:.0f}%) -- más probable error de modelo que ventaja real."
            )
            continue
        best = dict(cand)
        best["market_price"] = real_price
        best["ev"] = real_ev
        best["liquidity"] = real_liquidity
        best["price_source"] = "book_real"
        break

    return {
        "status": "ok",
        "type": "WEATHER_SIGNAL",
        "title": title,
        "url": event.get("url"),
        "station": station,
        "center_estimate_f": center,
        "sigma": round(sigma, 2),
        "confidence_penalty": penalty,
        "physics_notes": notes,
        "nws": nws,
        "metar": metar,
        "taf": taf,
        "buckets": rows,
        "best_trade": best,
        "discard_notes": discard_notes,
        "settlement_verified": station.get("verified", False),
        "min_ev_threshold": effective_min_ev,
        "disclaimer": DISCLAIMER,
    }


# ---------------------------------------------------------------------------
# STEP 4 — Formato de reporte (versión compacta para Telegram)
# ---------------------------------------------------------------------------
def _half_kelly_fraction(prob, price):
    """Fracción de Kelly (a mitad, por conservadurismo) para una apuesta
    binaria que paga $1 si gana y cuesta `price`. Solo informativo: el
    módulo de clima es de solo lectura y no ejecuta ni dimensiona órdenes
    en Polymarket (a diferencia de risk_manager.py en el módulo cripto)."""
    if not price or price <= 0 or price >= 1:
        return None
    kelly = (prob - price) / (1 - price)
    return max(0.0, kelly) / 2


def build_weather_memo(signal, markdown=True):
    """
    Versión compacta: solo lo necesario para decidir (estimación, mejor
    oportunidad, link). El desglose completo por bucket y la física del día
    siguen disponibles corriendo weather_report.py a mano.

    AUDITORÍA (03/09/2026): la versión anterior mostraba "Mi prob" y
    "Precio mercado" uno al lado del otro sin decir qué acción tomar --
    quien lee el mensaje tenía que inferir si eso era señal de compra y de
    qué lado. Ahora la primera línea después del header es siempre la
    decisión explícita (comprar SÍ + qué bucket, o no operar y por qué),
    con el edge en puntos porcentuales (que es lo que realmente justifica
    la operación, no el EV% solo) y el umbral que se usó para decidir.
    También se listan los próximos 2 buckets por EV con el motivo del
    descarte, para que se vea que el bot barrió todas las opciones.

    FIX: la versión anterior envolvía DISCLAIMER en asteriscos/guiones bajos
    para itálica (`_{disclaimer}_`), pero el texto del disclaimer contiene
    "weather_report.py" — el guion bajo de ahí adentro rompe el conteo de
    pares que necesita el parser de Markdown de Telegram (parse_mode
    "Markdown" en telegram_notifier.py), lo que corrompía el parseo de todo
    el mensaje y arruinaba el link de arriba. Se saca el disclaimer del
    mensaje en vez de escapar el guion bajo, porque además es ruido para
    decidir en el momento — cualquier texto con guion bajo/asterisco que se
    interpole en un mensaje Markdown puede repetir este bug si se
    reintroduce texto libre acá.
    """
    if not signal or signal.get("status") != "ok":
        return None
    st = signal["station"]
    threshold = signal.get("min_ev_threshold")
    lines = []
    title_txt = signal["title"][:70]
    lines.append(f"🌡️ *ANÁLISIS DE CLIMA* — {title_txt}" if markdown else f"🌡️ ANÁLISIS DE CLIMA — {title_txt}")
    lines.append("")
    verified_flag = "✅ verificada" if signal["settlement_verified"] else "⚠️ SIN VERIFICAR — revisar manualmente"
    lines.append(f"📍 Estación: {st.get('name', st.get('icao'))} ({verified_flag})")
    lines.append(f"📈 Estimación de máxima: {signal['center_estimate_f']}°F (±{signal['sigma']:.1f}°F)")
    lines.append("")

    rows = signal.get("buckets") or []
    bt = signal["best_trade"]
    if bt:
        edge_pp = (bt["my_prob"] - bt["market_price"]) * 100
        label = f"SEÑAL: COMPRAR \"SÍ\" — {bt['question'][:60]}"
        lines.append(f"🟢 {label}" if not markdown else f"🟢 *{label}*")
        lines.append(
            f"   Mi prob: {bt['my_prob']*100:.0f}% | Mercado: {bt['market_price']*100:.1f}¢ | "
            f"Edge: {edge_pp:+.0f}pp | EV: {bt['ev']*100:+.0f}%"
        )
        kelly = _half_kelly_fraction(bt["my_prob"], bt["market_price"])
        if kelly is not None:
            lines.append(f"   Tamaño sugerido (½ Kelly, informativo): {kelly*100:.1f}% del bankroll")
        if bt.get("url"):
            link_text = "Ver en Polymarket"
            lines.append(f"🔗 [{link_text}]({bt['url']})" if markdown else bt["url"])
    else:
        top_ev = rows[0]["ev"] if rows and rows[0].get("ev") is not None else None
        threshold_txt = f"{threshold*100:.0f}%" if threshold is not None else "N/A"
        discard_notes = signal.get("discard_notes") or []
        if discard_notes:
            # AUDITORÍA (04/09/2026): distinguir "nada superó el umbral"
            # (disciplina normal) de "algo superó el umbral pero no
            # sobrevivió la verificación real contra el book" (precio
            # viejo, sin liquidez, o EV sospechosamente alto) — son
            # diagnósticos distintos y el segundo es la señal de que el
            # modelo, no el mercado, es lo que hay que revisar.
            lines.append(f"⚪ SIN SEÑAL — {discard_notes[0]}")
        elif top_ev is not None:
            lines.append(f"⚪ SIN SEÑAL — mejor EV disponible es {top_ev*100:+.0f}%, por debajo del umbral mínimo ({threshold_txt})")
        else:
            lines.append("⚪ SIN SEÑAL — pasar es la disciplina correcta.")
        if signal.get("url"):
            link_text = "Ver evento en Polymarket"
            lines.append(f"🔗 [{link_text}]({signal['url']})" if markdown else signal["url"])

    # Próximos candidatos descartados, para que se vea que el bot barrió
    # todas las opciones y la elección no es arbitraria.
    others = [r for r in rows if not bt or r["condition_id"] != bt["condition_id"]][:2]
    if others:
        lines.append("")
        lines.append("📊 Otros rangos evaluados:")
        for r in others:
            ev_txt = f"{r['ev']*100:+.0f}%" if r.get("ev") is not None else "N/A"
            if r.get("ev") is not None and threshold is not None and r["ev"] < threshold:
                reason = "no cumple umbral"
            elif r.get("ev") is not None and bt and r["ev"] <= bt["ev"]:
                reason = "EV menor"
            else:
                reason = "descartado"
            lines.append(f"   {r['question'][:45]:<45} → {r['my_prob']*100:.0f}% vs {r['market_price']*100:.1f}¢ (EV {ev_txt}, {reason})")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Deduplicación de avisos entre ciclos (mismo patrón que PolymarketStateStore,
# pero backed por Database.get_state/set_state en vez de un JSON en disco —
# necesario en serverless porque el filesystem no persiste entre invocaciones)
# ---------------------------------------------------------------------------
STATE_KEY = "weather_notify_state"


class WeatherNotifyStateStore:
    def __init__(self, db, resend_cooldown_hours=3.0, min_ev_increase_pct=0.20):
        self.db = db
        self.resend_cooldown_seconds = resend_cooldown_hours * 3600
        self.min_ev_increase_pct = min_ev_increase_pct

    def _load(self):
        import json
        raw = self.db.get_state(STATE_KEY, "{}")
        try:
            return json.loads(raw) if raw else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    def _save(self, state):
        import json
        self.db.set_state(STATE_KEY, json.dumps(state))

    def should_notify(self, condition_id, ev):
        state = self._load()
        prev = state.get(condition_id)
        if prev is None:
            return True
        elapsed = time.time() - prev.get("ts", 0)
        if elapsed >= self.resend_cooldown_seconds:
            return True
        prev_ev = prev.get("ev", 0) or 0
        if prev_ev > 0 and ev >= prev_ev * (1 + self.min_ev_increase_pct):
            return True
        return False

    def record_notified(self, condition_id, ev):
        state = self._load()
        state[condition_id] = {"ts": time.time(), "ev": ev}
        self._save(state)
