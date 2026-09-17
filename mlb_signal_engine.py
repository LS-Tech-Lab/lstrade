"""
Motor de análisis de béisbol (MLB) para mercados moneyline de Polymarket
("Will the X beat the Y?" / "X vs Y" -- outcomes literales con el nombre
de cada equipo, ver parse_market_for_analysis() en polymarket_client.py).

Combina, ANTES de mirar el precio de Polymarket (mismo espíritu que
weather_signal_engine.py con NWS/METAR/TAF):
  1. Forma de equipo: win% de temporada + récord de los últimos 10
     (MLB Stats API /standings)
  2. Calidad del pitcher probable de cada lado (ERA de temporada,
     MLB Stats API /people/{id}/stats)
  3. Ventaja de local (ajuste fijo, MLB_HOME_FIELD_EDGE)

con la fórmula log5 (Bill James) como base, para armar una probabilidad de
fundamentos independiente y compararla contra el precio real.

Fuente de datos: MLB Stats API (statsapi.mlb.com) -- pública, sin API key,
ver https://github.com/pseudo-r/Public-MLB-API. Gratis para uso individual
o no-masivo según los términos de MLB Advanced Media; este módulo solo
consulta el partido puntual que ya matcheó un mercado de Polymarket, no
hace scraping en bulk de temporadas completas.

AUDITORÍA (04/09/2026): PITCHER_ERA_SCALE y HOME_FIELD_EDGE de abajo son
valores de arranque sin calibrar todavía contra resultados reales -- mismo
punto en el que estaba WEATHER_BASE_SIGMA_F antes de tener señales
resueltas para backtestear. Revisar apenas haya un puñado de semanas de
señales de mlb_signals resueltas.

Este módulo es puro (sin I/O de Telegram/DB), igual que
weather_signal_engine.py -- el orquestador (api/mlb_cycle.py, todavía no
armado) importa de acá.

TODO antes de conectar a producción: verificar contra la API real (no
alcanzable desde este entorno de desarrollo) que los nombres de campo de
/schedule, /standings y /people/{id}/stats coinciden exactamente con lo
que se asume acá -- están tomados de documentación de terceros
(pseudo-r/Public-MLB-API), no de una respuesta real inspeccionada.
"""
import logging
import math
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from weather_signal_engine import compute_ev, _half_kelly_fraction
from polymarket_signal_engine import analyze_probability_momentum, detect_inefficiency
from config import Config

log = logging.getLogger("mlb_signal_engine")

MLB_API = "https://statsapi.mlb.com/api/v1"
DEFAULT_TIMEOUT = 6
AL_LEAGUE_ID = 103
NL_LEAGUE_ID = 104

# AUDITORÍA (05/09/2026): MLB define el "día de partido" en huso horario
# US/Eastern (así lo etiqueta la propia MLB en sus páginas públicas de
# schedule/lineups -- "All Times Eastern"), no en UTC. El servidor donde
# corre esto (función serverless de Vercel) usa UTC, que está 4-5 horas
# adelantado de Eastern según horario de verano. La franja en la que UTC
# ya cruzó medianoche pero en el Este de EE.UU. todavía es "ayer" es
# exactamente 20:00-02:00 UTC (h. verano) / 21:00-03:00 UTC (h. invierno)
# -- es decir, el horario pico de partidos de MLB (7-10pm ET). Antes esto
# se resolvía con `time.strftime("%Y-%m-%d")` (fecha del servidor, UTC),
# así que durante esa franja fetch_probable_pitchers_for_date() pedía la
# fecha de MAÑANA -- typicamente sin partidos programados todavía o con un
# cruce distinto de equipos -- y generate_mlb_signal() no encontraba el
# partido real que se estaba jugando en ese momento (devuelve None en
# "no hay partido HOY entre estos dos equipos"), perdiendo la señal
# durante buena parte del horario en que más partidos hay en curso.
MLB_SCHEDULE_TZ = ZoneInfo("America/New_York")


def current_mlb_date():
    """Fecha de "hoy" para efectos de schedule de MLB, en huso horario
    US/Eastern -- ver AUDITORÍA arriba. Usar esto (no time.strftime) en
    cualquier lugar que arme la fecha para /schedule o para el `season`
    por defecto."""
    return datetime.now(MLB_SCHEDULE_TZ).strftime("%Y-%m-%d")


def fetch_game_result(game_pk, timeout=DEFAULT_TIMEOUT):
    """Resultado final de un partido puntual por gamePk, para resolver una
    señal ya generada -- NUEVO (06/09/2026): generate_mlb_signal() existía
    desde ayer pero nada en el repo consultaba si el partido ya terminó
    para cerrar la señal (a diferencia de weather_track_results.py y
    polymarket_track_results.py, que sí existen para clima/Polymarket);
    las señales de MLB se quedaban abiertas para siempre. Esto es la
    pieza que faltaba conectar.

    Devuelve None si el partido todavía no terminó (en curso, pospuesto,
    suspendido) o si falla la llamada -- el caller debe reintentar en el
    próximo ciclo, mismo contrato que fetch_clob_market()/`closed` en el
    flujo de clima."""
    try:
        resp = requests.get(
            f"{MLB_API}/schedule",
            # FIX (07/09/2026): el parámetro real de la MLB Stats API para
            # filtrar por partido puntual es "gamePks" (plural) -- "gamePk"
            # (singular, lo que había acá) no es un parámetro que la API
            # reconozca, así que /schedule lo ignoraba por completo. Sin
            # "date"/"startDate" tampoco puesto, la respuesta volvía sin
            # "dates" (lista vacía) SIEMPRE, para cualquier gamePk, real o
            # no -- esto es lo que hacía que un partido terminado hace
            # horas nunca se detectara como Final (se confirmó con
            # still_open=10, resolved=[] reportado en producción: todas
            # las señales fallan de la misma manera, no solo casos borde).
            # Se agrega también "sportId" (requerido por la API para
            # devolver resultados, mismo patrón que ya usa
            # fetch_probable_pitchers_for_date más abajo en este archivo).
            params={"gamePks": game_pk, "sportId": 1, "hydrate": "linescore"},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning(f"Error fetching game result for gamePk={game_pk}: {e}")
        return None

    games = [g for d in data.get("dates", []) for g in d.get("games", [])]
    if not games:
        return None
    game = games[0]
    # FIX (07/09/2026): "detailedState != 'Final'" exigía ese string exacto,
    # pero la MLB Stats API usa varios detailedState distintos para un
    # partido ya terminado (p.ej. "Game Over" en partidos suspendidos que
    # se retoman y cierran) -- todos esos casos comparten
    # abstractGameState == "Final" (los otros dos valores posibles son
    # "Preview" y "Live"), que es el campo pensado para esta pregunta.
    # Con el chequeo viejo, cualquier partido que terminara con un
    # detailedState distinto de "Final" quedaba reintentándose para
    # siempre -- la señal correspondiente nunca se cerraba.
    status = game.get("status", {})
    if status.get("abstractGameState") != "Final":
        return None  # en curso / todavía no empieza -- se reintenta en el próximo ciclo

    home = game.get("teams", {}).get("home", {})
    away = game.get("teams", {}).get("away", {})
    home_won, away_won = bool(home.get("isWinner")), bool(away.get("isWinner"))
    if not home_won and not away_won:
        # Partido cancelado/suspendido sin reanudar y sin ganador real (no
        # "Final" con resultado jugado) -- no hay nada que resolver como
        # ganancia o pérdida. Se marca aparte para que el caller decida qué
        # hacer (ej. anular la señal) en vez de contarlo como derrota por
        # default.
        return {
            "home_id": home.get("team", {}).get("id"), "away_id": away.get("team", {}).get("id"),
            "home_score": home.get("score"), "away_score": away.get("score"),
            "home_won": False, "away_won": False, "voided": True,
        }
    return {
        "home_id": home.get("team", {}).get("id"),
        "away_id": away.get("team", {}).get("id"),
        "home_score": home.get("score"),
        "away_score": away.get("score"),
        "home_won": home_won,
        "away_won": away_won,
        "voided": False,
    }

# AUDITORÍA (13/09/2026, backtest histórico -- 7303 partidos 2023-2025, ver
# backtest_mlb.py): HOME_FIELD_EDGE se sumaba directo a una probabilidad
# (prob += home_field_edge), lo cual no es matemáticamente prolijo -- empujar
# +0.04 a una probabilidad que ya viene alta por log5 la acerca
# desproporcionadamente al techo (0.05-0.95), mientras que a una probabilidad
# ya baja la empuja desproporcionadamente al piso. Confirmado en el backtest:
# la calibración por decil muestra sobreconfianza en AMBOS extremos, no solo
# arriba (0-10% predicho -> ~37-45% real; 90-100% predicho -> ~62-71% real),
# justo el patrón que produce un ajuste aditivo simple sobre una probabilidad
# ya extrema. Se mueve a espacio de log-odds (logit) -- ver
# to_log_odds/from_log_odds y combine_components() más abajo -- para que el
# mismo ajuste de localía empuje MENOS en términos de probabilidad cuando el
# partido ya está lejos de 50-50, en vez de empujar lo mismo en cualquier
# punto de la curva.
# Valor: 0.16 en log-odds equivale a mover un partido 50-50 a ~54% de local
# -- la ventaja de localía históricamente aceptada en MLB (ln(0.54/0.46) =
# 0.1604) -- mismo punto de referencia que ya se había usado para elegir
# 0.04 en espacio de probabilidad (0.5+0.04=0.54), solo que ahora expresado
# en las unidades correctas para que no se deforme lejos del 50-50.
HOME_FIELD_EDGE = 0.16       # en espacio de log-odds -- ver AUDITORÍA arriba (antes 0.04 en espacio de probabilidad)

# AUDITORÍA (12/09/2026, sesión con Claude a partir del panel de MLB
# mostrando 26.1% de acierto / calibración invertida en 60-90%): se aisló
# con datos reales de Supabase cuál de los dos componentes (pitcher_edge
# vs. el log5 de win% de equipo) explica más el exceso de confianza.
#
# 1) Sobre las 31 señales pre-clip con my_prob>=60% agrupadas por qué tan
#    extremo estaba pitcher_edge: el grupo con pitcher_edge en su techo
#    (±0.15, n=8) tuvo 0% de acierto real con 76.0% de confianza promedio
#    -- 8 de 8 perdidas. El grupo con pitcher_edge≈0 (n=18) también estuvo
#    mal (27.8% real contra 69.4% dicho), pero no tan extremo.
# 2) Contrafactual sobre las 22 señales con componentes guardados
#    (07/09 en adelante): recalculando my_prob SIN pitcher_edge (solo
#    log5 + localía), el Brier score mejora de 0.388 a 0.298 (~23% mejor)
#    -- confirma que pitcher_edge es el mayor contribuyente individual al
#    exceso de confianza, aunque no el único (0.298 sigue lejos de 0.25).
#
# Lectura: con MIN_INNINGS_FOR_ERA=15 el ERA de temporada de un probable
# puede estar dominado por 1-2 salidas atípicas, y además esa ERA ya está
# parcialmente reflejada en home_win_pct/away_win_pct (un equipo con buena
# rotación tiende a ganar más en la temporada) -- pitcher_edge puede estar
# contando dos veces la misma señal de "este equipo es bueno" en vez de
# aportar información independiente, y encima con más ruido.
# Se reduce el impacto (no se elimina -- SÍ hay señal real de "quién
# pitchea hoy", solo hay que exigirle más muestra y pesar menos su cola):
# MIN_INNINGS_FOR_ERA sube a 30 (ERA más confiable antes de usarla) y
# PITCHER_ERA_SCALE/el techo de pitcher_edge() bajan a la mitad. Revisar
# de nuevo con la próxima tanda de señales resueltas (idealmente 40-50
# más) para confirmar si esto ya corrige el Brier hacia 0.25 o si hace
# falta seguir bajando -- o mirar también SEASON_FORM_WEIGHT, que la
# contrafactual de arriba muestra que tampoco está limpio del todo
# (log5+localía solos siguen en Brier 0.298, no 0.25).
PITCHER_ERA_SCALE = 0.05     # AUDITORÍA 12/09/2026 arriba -- bajado de 0.10
PITCHER_EDGE_CAP = 0.08      # AUDITORÍA 12/09/2026 arriba -- bajado de 0.15 (antes hardcodeado en pitcher_edge())
SEASON_FORM_WEIGHT = 0.7     # peso de win% de temporada vs. últimos-10 en blended_win_pct
MIN_INNINGS_FOR_ERA = 30.0   # AUDITORÍA 12/09/2026 arriba -- subido de 15.0 (ERA de pocas salidas es ruido)
MOMENTUM_DISAGREEMENT_THRESHOLD = 0.08  # ver price_disagrees_with_model() -- sin calibrar

# AUDITORÍA (13/09/2026, backtest histórico -- ver backtest_mlb.py): el
# bucket de probabilidad predicha MÁS bajo (0-10%) resultó ganando el
# partido 45.1% de las veces en el dataset completo -- muy lejos de lo que
# dice el modelo. Se aisló la causa: 82/82 de esos partidos tenían al menos
# un equipo con win% de temporada en 0.0 o 1.0 exacto, es decir, muy pocos
# partidos jugados todavía (arranque de temporada) -- un equipo 0-3 no es
# "el peor equipo de la liga", es ruido de muestra chica que log5 toma como
# señal fuerte. Excluyendo las primeras 3 semanas de cada temporada, ese
# bucket bajó de 82 a 19 partidos y la calibración en los extremos mejoró
# (90-100% predicho pasó de 62.6% a 71.4% real). Mismo patrón, mismo tipo de
# fix, que MIN_INNINGS_FOR_ERA ya aplica del lado del pitcher -- acá el
# equivalente para el win% de EQUIPO: si un equipo todavía no jugó
# MIN_GAMES_FOR_FORM partidos en la temporada, blended_win_pct() lo trata
# como "sin forma" (50% neutral, mismo tratamiento que ya existía para
# start-of-season sin datos), en vez de confiar en una fracción de 1-14
# partidos. 15 es conservador a propósito (roughly 2-3 semanas a razón de
# ~6 partidos/semana, en línea con lo que confirmó el backtest) -- revisar
# si hace falta afinarlo con el próximo backtest.
# AUDITORÍA (14/09/2026, tercera corrida del backtest -- primera con FIP y
# Pythagorean ya activos): reemplaza al clip duro
# Config.MLB_PROB_CLIP_MIN/MAX=[0.40,0.60] del 09/09/2026. Platt scaling en
# espacio logit, fiteado con mlb_calibration.py sobre 7303 partidos
# (2023-2025), evaluado en un holdout cronológico de los últimos ~20%
# (1461 partidos desde 2025-06-08, nunca visto al fitear): Brier bajó de
# 0.2531 (crudo) a 0.2458 (calibrado) en ESE holdout. La pendiente sigue
# subiendo corrida a corrida (a=0.08 pre-fix de HOME_FIELD_EDGE/
# MIN_GAMES_FOR_FORM -> a=0.36 post-fix -> a=0.40 post-FIP/Pythagorean) --
# cada mejora estructural recupera más señal real, en la misma dirección
# las tres veces. Ver calibrate_prob() más abajo.
#
# AUDITORÍA (17/09/2026, cuarta corrida -- primera con pitcher_edge en
# espacio de log-odds, ver AUDITORÍA en combine_components()): mismo
# dataset (7303 partidos, holdout 1461 desde 2025-06-08), corrido por el
# usuario vía backtest-mlb.yml. Confirmado el efecto esperado del fix: en
# la calibración CRUDA (antes de este Platt) del holdout, el bucket
# 40-50% -- el mismo que mostraba -21pts de sobreconfianza en vivo en la
# versión e5b399eb, motivo original de este fix -- pasó a predicho 45.2%
# / real 50.4%, solo +5.2pts de gap. El Brier crudo agregado del holdout
# prácticamente no se mueve (0.2527 vs. 0.2531 antes) porque la mayoría de
# los partidos ya estaban cerca de 50-50 y el fix atenúa el shift
# principalmente lejos de ahí -- la mejora es real pero LOCAL a esa zona,
# no un salto agregado. Sigue habiendo sobreconfianza clara del lado
# favorito (60-90% predicho, real 7-12pts por debajo en cada bucket de esa
# franja) -- no se toca en este pase, queda para la próxima auditoría
# (candidato: mismo tipo de revisión que ya se le hizo a pitcher_edge, pero
# sobre FIP_CONSTANT/PYTHAGOREAN_EXPONENT o SEASON_FORM_WEIGHT). Platt
# completo sigue ganando en holdout (0.2459 calibrado vs. 0.2527 crudo) con
# pendiente casi sin cambios (a=0.4175 vs. 0.4018) -- esperable, dado que
# el fix corrige una distorsión LOCAL cerca de 40-50%, no la forma general
# de sobreconfianza en los extremos que Platt ya venía corrigiendo.
# Reemplazar estos dos valores la próxima vez que se corra
# mlb_calibration.py con datos más nuevos (el workflow backtest-mlb.yml ya
# corre esa calibración automáticamente al final de cada backtest).
CALIBRATION_A = 0.4175  # pendiente (logit-space) -- ver AUDITORÍA arriba (17/09/2026)
CALIBRATION_B = 0.0511  # shift (logit-space) -- ver AUDITORÍA arriba (17/09/2026)



MIN_GAMES_FOR_FORM = 15

# AUDITORÍA (13/09/2026, "Mejoras al modelo en sí" -- pedido del usuario):
# ERA de temporada depende de la defensa del equipo y de la suerte de
# bateo en juego (BABIP) del pitcher -- dos cosas que no controla. FIP
# (Fielding Independent Pitching) aísla lo que el pitcher SÍ controla
# (HR, BB, HBP, K) y es más predictivo partido a partido, que es
# justamente lo que necesita este modelo (una estimación de "qué tan
# bien va a tirar HOY", no un resumen retrospectivo de temporada).
# Fórmula estándar: FIP = (13*HR + 3*(BB+HBP) - 2*K) / IP + constante.
# La constante normalmente se recalcula por temporada a partir del ERA
# promedio de las ligas (para que FIP quede en la misma escala que ERA,
# ~4.00 de promedio) -- eso requeriría un fetch adicional de totales de
# liga que hoy no se está haciendo. Se usa un valor fijo ~3.10 (rango
# histórico real ~3.0-3.3 según la temporada) como aproximación razonable
# -- más simple que consultar totales de liga por temporada, a costa de
# un pequeño corrimiento de escala año a año. Si hace falta más precisión,
# el próximo paso es calcular la constante real por temporada desde
# /api/v1/stats?stats=season&group=pitching&sportId=1&season=YYYY
# (totales de liga), no está hecho todavía.
FIP_CONSTANT = 3.10

# AUDITORÍA (13/09/2026): Pythagorean win expectancy (diferencial de
# carreras) en vez del win% crudo de temporada para el componente de
# "forma de equipo" -- un récord W-L puede estar inflado/desinflado por
# partidos ganados/perdidos por una carrera (alta varianza, no repite),
# mientras que el diferencial de carreras es más estable y predictivo de
# la fuerza real del equipo. Exponente 1.83 (el refinamiento de Bill James
# sobre el clásico exponente 2 para MLB específicamente -- ver
# pythagorean_win_pct() más abajo) en vez del win% de temporada, no en vez
# del de últimos-10 (una ventana de 10 partidos es chica para que el
# diferencial de carreras sea más estable que el récord real -- ahí se
# sigue usando el récord real tal cual).
PYTHAGOREAN_EXPONENT = 1.83

# AUDITORÍA (13/09/2026): las otras dos mejoras pedidas -- bullpen
# (ERA/FIP del staff de relevo) y factores de parque -- se evalúan y NO se
# implementan hoy:
#   - Factores de parque: se buscaron valores reales antes de hardcodear
#     nada (ver conversación) -- las fuentes públicas discrepan fuerte
#     entre sí para el mismo parque/temporada (ej. Coors Field: 107, 110,
#     128 y 138 según el sitio y la ventana usada). Meter una tabla
#     estática con números que ni las fuentes externas acuerdan entre sí
#     sería inyectar un sesgo mal sustentado y silencioso en cada
#     predicción. El camino correcto es derivarlos de los mismos datos que
#     ya junta backtest_mlb.py (carreras anotadas en cada parque vs. el
#     resto de la liga, con suficiente muestra) -- no está hecho todavía.
#   - Bullpen: la MLB Stats API no tiene (que se haya podido confirmar
#     desde este entorno, sin acceso a statsapi.mlb.com para probar) un
#     endpoint simple y confiable para "ERA/FIP solo de relevo" separado
#     del abridor -- requeriría clasificar cada pitcher del roster por rol
#     y sumar sus starts/relief appearances a mano, con bastante superficie
#     para errores silenciosos si se adivina el campo/endpoint equivocado.
#     Mismo criterio que ya se usó con MLB_EXTREME_PRICE_FLOOR y otros
#     campos no verificados: no adivinar un endpoint crítico sin poder
#     confirmarlo contra una respuesta real.

# AUDITORÍA (13/09/2026): ya van dos veces (el clip de probabilidad del
# 09/09 y este mismo ajuste de pitcher_edge del 12/09) que un cambio de
# constante se mezcla en el dashboard con señales generadas ANTES del
# cambio, porque no había ningún campo en mlb_signals que dijera con qué
# configuración se generó cada una -- había que ir a `git log`, encontrar
# el commit y hardcodear una fecha de corte en route.js (ver
# MLB_CALIBRATION_FIX_CUTOFF, ya retirado). MODEL_VERSION es un hash corto
# (8 hex) de TODAS las constantes que afectan estimate_win_probability()/
# el clip/el piso de precio, calculado una sola vez al importar este
# módulo. Se guarda en cada fila de mlb_signals (ver record_mlb_signal en
# supabase_db.py) para que el dashboard pueda agrupar/calibrar por versión
# automáticamente sin arqueología de git ni cutoffs a mano cada vez que se
# toque una constante acá o en Config. Nota: es un fingerprint de los
# VALORES activos (incluye overrides por env var de Config), no del commit
# -- dos commits distintos con los mismos valores activos comparten
# versión, y un mismo commit con un env var distinto en producción no.
# AUDITORÍA (17/09/2026): MODEL_VERSION fingerprinteaba solo VALORES de
# constantes -- correcto para detectar que alguien subió HOME_FIELD_EDGE de
# 0.12 a 0.16, pero ciego a un cambio de FÓRMULA que deja las constantes
# intactas (como el fix de pitcher_edge a espacio de log-odds, ver
# AUDITORÍA en combine_components()): las señales de ANTES y DESPUÉS de
# ese fix habrían compartido el mismo hash, mezclándose en el dashboard sin
# forma de distinguirlas -- exactamente el problema que MODEL_VERSION
# existe para evitar (ver AUDITORÍA 13/09/2026 más arriba). Se agrega este
# literal al fingerprint, a subir a mano cada vez que se toque la
# SECUENCIA de combine_components()/calibrate_prob() (no sus constantes,
# esas ya se fingerprintean solas) sin cambiar ningún valor.
MODEL_FORMULA_VERSION = 2  # 1 = pitcher_edge en espacio de probabilidad; 2 = en espacio de log-odds (17/09/2026)


def _compute_model_version():
    import hashlib
    fingerprint = "|".join(str(v) for v in [
        HOME_FIELD_EDGE, PITCHER_ERA_SCALE, PITCHER_EDGE_CAP, SEASON_FORM_WEIGHT,
        MIN_INNINGS_FOR_ERA, MIN_GAMES_FOR_FORM, MOMENTUM_DISAGREEMENT_THRESHOLD,
        CALIBRATION_A, CALIBRATION_B, FIP_CONSTANT, PYTHAGOREAN_EXPONENT,
        Config.MLB_EXTREME_PRICE_FLOOR, MODEL_FORMULA_VERSION,
    ])
    return hashlib.sha256(fingerprint.encode()).hexdigest()[:8]

MODEL_VERSION = _compute_model_version()

# id MLB -> (nombre completo, nombre corto/"teamName", abreviatura)
# fuente: https://github.com/pseudo-r/Public-MLB-API (docs/teams.md)
TEAMS = {
    108: ("Los Angeles Angels", "Angels", "LAA"),
    109: ("Arizona Diamondbacks", "Diamondbacks", "ARI"),
    110: ("Baltimore Orioles", "Orioles", "BAL"),
    111: ("Boston Red Sox", "Red Sox", "BOS"),
    112: ("Chicago Cubs", "Cubs", "CHC"),
    113: ("Cincinnati Reds", "Reds", "CIN"),
    114: ("Cleveland Guardians", "Guardians", "CLE"),
    115: ("Colorado Rockies", "Rockies", "COL"),
    116: ("Detroit Tigers", "Tigers", "DET"),
    117: ("Houston Astros", "Astros", "HOU"),
    118: ("Kansas City Royals", "Royals", "KC"),
    119: ("Los Angeles Dodgers", "Dodgers", "LAD"),
    120: ("Washington Nationals", "Nationals", "WSH"),
    121: ("New York Mets", "Mets", "NYM"),
    133: ("Oakland Athletics", "Athletics", "ATH"),
    134: ("Pittsburgh Pirates", "Pirates", "PIT"),
    135: ("San Diego Padres", "Padres", "SD"),
    136: ("Seattle Mariners", "Mariners", "SEA"),
    137: ("San Francisco Giants", "Giants", "SF"),
    138: ("St. Louis Cardinals", "Cardinals", "STL"),
    139: ("Tampa Bay Rays", "Rays", "TB"),
    140: ("Texas Rangers", "Rangers", "TEX"),
    141: ("Toronto Blue Jays", "Blue Jays", "TOR"),
    142: ("Minnesota Twins", "Twins", "MIN"),
    143: ("Philadelphia Phillies", "Phillies", "PHI"),
    144: ("Atlanta Braves", "Braves", "ATL"),
    145: ("Chicago White Sox", "White Sox", "CWS"),
    146: ("Miami Marlins", "Marlins", "MIA"),
    147: ("New York Yankees", "Yankees", "NYY"),
    158: ("Milwaukee Brewers", "Brewers", "MIL"),
}

# nombre (en cualquiera de sus 3 formas, en minúscula) -> team_id, para
# resolver un outcome label de Polymarket contra un id real. Se ordena por
# longitud descendente al buscar (ver resolve_team_id) para que "red sox"
# matchee antes que cualquier substring corta ambigua.
_NAME_TO_ID = {}
for _id, _names in TEAMS.items():
    for _n in _names:
        _NAME_TO_ID[_n.lower()] = _id


def _get(path, params=None, timeout=DEFAULT_TIMEOUT):
    try:
        r = requests.get(f"{MLB_API}{path}", params=params or {}, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning(f"MLB Stats API falló en {path}: {e}")
        return None


_MLB_TAG_ID_CACHE = {"id": None, "resolved_at": 0.0}
_MLB_TAG_ID_TTL_SECONDS = 86400.0  # el tag_id de una liga no cambia -- 24h de caché


def resolve_mlb_tag_id(client):
    """Cachea (por proceso, TTL 24h) el tag_id de MLB resuelto vía
    client.resolve_tag_id('mlb') -- /tags/slug/mlb en Gamma.

    AUDITORÍA (05/09/2026): antes, run_mlb_cycle() escaneaba el top-N de
    fetch_active_markets ordenado por volume24hr de TODO Polymarket, sin
    filtro de liga. Se detectó en producción un ciclo real con
    games_today=15 y markets_scanned=0 -- los mercados de MLB de temporada
    regular tienen volumen bajo frente a cripto/política y simplemente no
    entraban en ese top-N. Mismo problema (y misma solución) que ya se
    había resuelto para clima con WEATHER_TAG_ID -- ver
    polymarket_client.py. A diferencia de WEATHER_TAG_ID, el de MLB no se
    hardcodea porque no está confirmado a mano contra una respuesta real
    todavía; se resuelve en vivo la primera vez y se cachea acá.

    Si el cold start de una serverless function resetea este cache en
    memoria, se vuelve a resolver -- un request extra ocasional, no un
    problema funcional."""
    now = time.monotonic()
    if _MLB_TAG_ID_CACHE["id"] is not None and (now - _MLB_TAG_ID_CACHE["resolved_at"]) < _MLB_TAG_ID_TTL_SECONDS:
        return _MLB_TAG_ID_CACHE["id"]
    tag_id = client.resolve_tag_id("mlb")
    if tag_id is not None:
        _MLB_TAG_ID_CACHE["id"] = tag_id
        _MLB_TAG_ID_CACHE["resolved_at"] = now
    return tag_id


def resolve_team_id(label):
    """
    Devuelve el team_id de MLB que matchea un outcome label de Polymarket
    (ej. "New York Yankees", "Yankees", "NYY"), o None si no reconoce
    ningún equipo de MLB en el texto -- esto ES el filtro de "¿esto es un
    mercado de MLB?", no hace falta una categoría aparte para eso.
    """
    if not label:
        return None
    low = label.lower()
    for name in sorted(_NAME_TO_ID, key=len, reverse=True):
        if name in low:
            return _NAME_TO_ID[name]
    return None


# AUDITORÍA (05/09/2026, usuario reportó 18 señales abiertas para 15
# partidos del día): resolve_team_id() de arriba SOLO confirma que hay dos
# nombres de equipo distintos en yes_label/no_label -- eso también es
# cierto para mercados derivados del mismo partido que NO son moneyline
# ("Spread: Seattle Mariners (-1.5)", con outcomes=["Seattle Mariners",
# "Oakland Athletics"] igual que el moneyline real de ese mismo partido).
# Confirmado en producción (Supabase, tabla mlb_signals): 6 de las 18
# señales abiertas eran mercados "Spread: ..." -- el modelo de fundamentos
# calcula P(el equipo gana el partido), que es un número MÁS ALTO que
# P(el equipo gana por 2+ carreras) exigido por un mercado de -1.5, así
# que aplicar esa probabilidad para pricear el spread infla el EV
# calculado sin que exista ventaja real -- mismo patrón de fondo que la
# categoría 1 del prompt de auditoría (probabilidad de una pregunta
# aplicada para pricear una pregunta distinta), aunque acá no es una
# cuenta de unidades sino de qué evento se está pricenado.
#
# Los derivados observados en producción se identifican todos por un
# calificador de línea entre paréntesis o antes del nombre del equipo
# ("Spread: ...", "Total: ...", "O/U ...") -- el moneyline real es
# sencillamente "Equipo A vs. Equipo B" sin calificador ni número.
_NON_MONEYLINE_KEYWORDS = ("spread", "total:", " o/u", "over/under", "run line", "moneyline -")


def is_moneyline_question(question, home_id, away_id):
    """True si `question` es el moneyline real "A vs B" de este partido, no
    un mercado derivado (spread/línea de carreras/total) que también trae
    los dos nombres de equipo y por lo tanto pasa resolve_team_id() igual
    que el moneyline -- ver AUDITORÍA arriba.

    Usa el nombre CORTO de TEAMS (ej. "Athletics", no "Oakland Athletics")
    para el chequeo de presencia: Polymarket a veces arma la pregunta del
    moneyline con el nombre corto ("Athletics vs. Seattle Mariners"), y
    exigir el nombre completo ahí rechazaba moneylines reales."""
    if not question:
        return False
    q = question.lower()
    if any(kw in q for kw in _NON_MONEYLINE_KEYWORDS):
        return False
    # Blindaje adicional: cualquier derivado con línea numérica entre
    # paréntesis (formato típico de spread/total, ej. "(-1.5)", "(O/U 8.5)")
    # -- un moneyline real nunca lleva un paréntesis con número.
    if re.search(r"\(-?\d", question):
        return False
    home_short = TEAMS[home_id][1].lower()
    away_short = TEAMS[away_id][1].lower()
    return home_short in q and away_short in q and " vs" in q


def fetch_probable_pitchers_for_date(date_str):
    """
    Todos los partidos de MLB de una fecha (YYYY-MM-DD) con el pitcher
    probable de cada lado. Pensado para llamarse UNA vez por ciclo (no una
    vez por mercado) y pasar el resultado como `today_games` a
    generate_mlb_signal() para todos los mercados de ese ciclo.
    """
    data = _get("/schedule", {"sportId": 1, "date": date_str, "hydrate": "probablePitcher"})
    if not data:
        return []
    games = []
    for d in data.get("dates", []):
        for g in d.get("games", []):
            teams = g.get("teams", {})
            home = teams.get("home", {})
            away = teams.get("away", {})
            games.append({
                "game_pk": g.get("gamePk"),
                "home_id": home.get("team", {}).get("id"),
                "away_id": away.get("team", {}).get("id"),
                "home_pitcher_id": (home.get("probablePitcher") or {}).get("id"),
                "away_pitcher_id": (away.get("probablePitcher") or {}).get("id"),
            })
    return games


def _parse_innings_pitched(ip_str):
    """"X.Y" de la MLB Stats API -- Y son TERCIOS de entrada (0, 1 o 2), no
    decimales: "6.2" es 6 y 2/3 = 6.667 entradas reales, no 6.2.

    NUEVO (13/09/2026): movido acá desde backtest_mlb.py -- fetch_pitcher_fip()
    (más abajo) necesita sumar IP a mano para el cálculo de FIP (a
    diferencia del viejo fetch_pitcher_era(), que usaba el campo "era" ya
    calculado por la propia API), así que ahora el motor en vivo también
    necesita esta conversión, no solo el backtest. Único lugar de verdad
    para los dos, mismo motivo que combine_components()."""
    if ip_str is None:
        return 0.0
    try:
        whole_str, _, frac_str = str(ip_str).partition(".")
        whole = float(whole_str) if whole_str else 0.0
        thirds = int(frac_str) if frac_str else 0
    except (ValueError, TypeError):
        return 0.0
    return whole + thirds / 3.0


def pythagorean_win_pct(runs_scored, runs_allowed, exponent=PYTHAGOREAN_EXPONENT):
    """Win% esperado a partir del diferencial de carreras -- ver
    AUDITORÍA junto a PYTHAGOREAN_EXPONENT. None si faltan datos o si
    alguno de los dos es 0 (imposible en un caso real con partidos
    jugados, pero defensivo por si la API devuelve algo raro)."""
    if runs_scored is None or runs_allowed is None or runs_scored <= 0 or runs_allowed <= 0:
        return None
    rs_exp = runs_scored ** exponent
    ra_exp = runs_allowed ** exponent
    return rs_exp / (rs_exp + ra_exp)


def fetch_team_form(team_id, season):
    """Win% de temporada y récord de los últimos 10 para un equipo, desde
    /standings (trae las dos ligas juntas, se filtra al id pedido)."""
    data = _get("/standings", {
        "leagueId": f"{AL_LEAGUE_ID},{NL_LEAGUE_ID}",
        "season": season,
        "standingsTypes": "regularSeason",
    })
    if not data:
        return None
    for record_group in data.get("records", []):
        for team_record in record_group.get("teamRecords", []):
            if team_record.get("team", {}).get("id") == team_id:
                last_ten_pct = None
                m = re.match(r"(\d+)-(\d+)", team_record.get("lastTen", "") or "")
                if m:
                    w, l = int(m.group(1)), int(m.group(2))
                    if w + l > 0:
                        last_ten_pct = w / (w + l)
                # AUDITORÍA (13/09/2026): games_played -- ver MIN_GAMES_FOR_FORM
                # arriba. Se intenta "gamesPlayed" directo primero (si la API lo
                # trae en teamRecord); si no, se lo deriva de wins+losses.
                # Ninguno de los dos nombres de campo está confirmado contra una
                # respuesta real (ver TODO en el header del módulo) -- si
                # ninguno existe, games_played queda None y blended_win_pct()
                # simplemente no aplica el filtro (se degrada al comportamiento
                # de antes, no rompe nada).
                games_played = team_record.get("gamesPlayed")
                if games_played is None:
                    w_total, l_total = team_record.get("wins"), team_record.get("losses")
                    if w_total is not None and l_total is not None:
                        games_played = w_total + l_total
                # AUDITORÍA (13/09/2026): runsScored/runsAllowed -- ver
                # pythagorean_win_pct()/PYTHAGOREAN_EXPONENT arriba. Mismos
                # nombres de campo sin confirmar contra una respuesta real
                # (ver TODO en el header del módulo); si no existen,
                # pyth_win_pct queda None y blended_win_pct() se degrada al
                # win% crudo de siempre, no rompe nada.
                runs_scored = team_record.get("runsScored")
                runs_allowed = team_record.get("runsAllowed")
                pyth_win_pct = pythagorean_win_pct(runs_scored, runs_allowed)
                return {
                    "win_pct": float(team_record.get("winningPercentage", 0.5) or 0.5),
                    "last_ten_pct": last_ten_pct,
                    "games_played": games_played,
                    "pyth_win_pct": pyth_win_pct,
                }
    return None


def fetch_pitcher_era(person_id, season):
    """ERA de temporada de un pitcher. LEGACY (13/09/2026) -- ya no se usa
    en el path en vivo (ver fetch_pitcher_fip() abajo, que la reemplaza en
    estimate_win_probability()); se deja definida por si hace falta
    comparar FIP vs. ERA más adelante. None si no encuentra el stat o si
    todavía no acumuló MIN_INNINGS_FOR_ERA (muestra chica -- una mala
    salida de debut no debería dominar el ajuste)."""
    if not person_id:
        return None
    data = _get(f"/people/{person_id}/stats", {"stats": "season", "group": "pitching", "season": season})
    if not data:
        return None
    for entry in data.get("stats", []):
        for split in entry.get("splits", []):
            stat = split.get("stat", {})
            try:
                innings = float(stat.get("inningsPitched", 0) or 0)
                era = float(stat.get("era"))
            except (TypeError, ValueError):
                return None
            if innings < MIN_INNINGS_FOR_ERA:
                return None
            return era
    return None


def fetch_pitcher_fip(person_id, season):
    """FIP de temporada de un pitcher (ver AUDITORÍA junto a FIP_CONSTANT)
    -- reemplaza a fetch_pitcher_era() en estimate_win_probability().
    A diferencia de ERA (que la API ya trae calculada), FIP hay que
    armarlo acá a partir de HR/BB/HBP/K/IP -- por eso necesita
    _parse_innings_pitched() para el formato "X.Y" en tercios de entrada.
    Mismo umbral MIN_INNINGS_FOR_ERA y mismo motivo que antes (una mala
    salida de muestra chica no debería dominar el ajuste). None si falta
    algún campo o no llegó a la muestra mínima."""
    if not person_id:
        return None
    data = _get(f"/people/{person_id}/stats", {"stats": "season", "group": "pitching", "season": season})
    if not data:
        return None
    for entry in data.get("stats", []):
        for split in entry.get("splits", []):
            stat = split.get("stat", {})
            try:
                innings = _parse_innings_pitched(stat.get("inningsPitched"))
                hr = float(stat.get("homeRuns", 0) or 0)
                bb = float(stat.get("baseOnBalls", 0) or 0)
                hbp = float(stat.get("hitByPitch", 0) or 0)
                so = float(stat.get("strikeOuts", 0) or 0)
            except (TypeError, ValueError):
                return None
            if innings < MIN_INNINGS_FOR_ERA or innings <= 0:
                return None
            fip = (13 * hr + 3 * (bb + hbp) - 2 * so) / innings + FIP_CONSTANT
            return round(fip, 2)
    return None


def blended_win_pct(form, season_weight=SEASON_FORM_WEIGHT, min_games=MIN_GAMES_FOR_FORM):
    """Mezcla la forma de temporada (Pythagorean si está disponible --
    ver AUDITORÍA junto a PYTHAGOREAN_EXPONENT, si no win% crudo como
    antes) con la forma de los últimos 10 (siempre récord real -- una
    ventana de 10 partidos es chica para que el diferencial de carreras
    sea más estable que el récord real ahí). Sin datos de últimos 10
    (arranque de temporada), usa solo el de temporada. Sin ningún dato, o
    con menos de min_games partidos jugados todavía en la temporada (ver
    AUDITORÍA en MIN_GAMES_FOR_FORM -- un win% de 1-14 partidos es ruido,
    no forma real), devuelve 50% neutral y lo marca como faltante -- mismo
    tratamiento en los dos casos, para que generate_mlb_signal() exija más
    EV cuando el modelo no tiene con qué respaldar la estimación, no solo
    cuando falta el dato por completo."""
    if form is None:
        return 0.5, True
    games_played = form.get("games_played")
    if games_played is not None and games_played < min_games:
        return 0.5, True
    season_pct = form.get("pyth_win_pct") if form.get("pyth_win_pct") is not None else form["win_pct"]
    if form["last_ten_pct"] is None:
        return season_pct, False
    return season_weight * season_pct + (1 - season_weight) * form["last_ten_pct"], False


def log5(pct_a, pct_b):
    """Fórmula de Bill James: prob. de que A le gane a B dados sus win%
    "verdaderos", sin ajuste de local ni de pitcher todavía."""
    denom = pct_a + pct_b - 2 * pct_a * pct_b
    if denom <= 0:
        return 0.5
    return (pct_a - pct_a * pct_b) / denom


def to_log_odds(p, eps=1e-6):
    """logit(p), con clamp a (eps, 1-eps) para no romper con p=0 o p=1 --
    log5() ya nunca devuelve exactamente 0 o 1 con inputs válidos, pero el
    clamp es barato y evita una excepción rara en un edge case no previsto."""
    p = min(max(p, eps), 1 - eps)
    return math.log(p / (1 - p))


def from_log_odds(x):
    """Inversa de to_log_odds -- sigmoide."""
    return 1.0 / (1.0 + math.exp(-x))


# AUDITORÍA (14/09/2026): valores CALIBRATION_A/CALIBRATION_B definidos
# más arriba (junto a MIN_GAMES_FOR_FORM, antes de MODEL_VERSION -- el
# fingerprint los necesita ya calculados) -- ver AUDITORÍA completa ahí
# (tercera corrida del backtest, primera con FIP/Pythagorean, a=0.40).
# Reemplazan al clip duro Config.MLB_PROB_CLIP_MIN/MAX=[0.40,0.60] del
# 09/09/2026 por Platt scaling en espacio logit. A diferencia del clip
# viejo, esto SÍ deja que un 55% y un 58% de log5+pitcher_edge sigan
# siendo distintos después de calibrar, solo que con niveles honestos.
def calibrate_prob(raw_prob, a=CALIBRATION_A, b=CALIBRATION_B):
    """Platt scaling en espacio logit -- calibrated = sigmoid(a*logit(raw)+b).
    Reemplaza al clip fijo Config.MLB_PROB_CLIP_MIN/MAX. Ver AUDITORÍA
    arriba y mlb_calibration.py para cómo se fitearon a/b."""
    return from_log_odds(a * to_log_odds(raw_prob) + b)


def pitcher_edge(era_a, era_b, scale=PITCHER_ERA_SCALE, cap=PITCHER_EDGE_CAP):
    """Diferencia de ERA entre los dos probables -> ajuste de probabilidad
    a favor de A. Positivo si A tiene mejor (más bajo) ERA. Cap a ±cap
    (ver PITCHER_EDGE_CAP y AUDITORÍA 12/09/2026 junto a PITCHER_ERA_SCALE)
    para que un mismatch de ERA extremo no domine por sí solo toda la
    estimación -- mismo espíritu que el cap de sanidad al final de
    estimate_win_probability."""
    if era_a is None or era_b is None:
        return 0.0
    return max(-cap, min(cap, (era_b - era_a) * scale))


def _prob_shift_to_log_odds(prob_shift):
    """Convierte un shift de probabilidad DEFINIDO EN REFERENCIA A UN 50-50
    (ej. 0.08 significa "un partido 50-50 pasa a 58%") al delta equivalente
    en espacio de log-odds -- mismo criterio que ya se usó para expresar
    HOME_FIELD_EDGE en estas unidades (ver AUDITORÍA 13/09/2026 junto a esa
    constante: 0.16 en log-odds se eligió porque equivale a mover un 50-50
    a ~54%, el mismo punto de referencia que antes daba 0.04 en espacio de
    probabilidad). Se usa acá para pitcher_edge -- ver AUDITORÍA 17/09/2026
    en combine_components().

    Clampeado a ±0.499 antes de convertir para no mandar to_log_odds() a
    ±infinito si algún día cap/scale se subieran por encima de 0.5 (no
    debería pasar con los valores actuales de PITCHER_EDGE_CAP, es solo
    defensa en profundidad barata)."""
    prob_shift = max(-0.499, min(0.499, prob_shift))
    return to_log_odds(0.5 + prob_shift) - to_log_odds(0.5)


def combine_components(home_pct, away_pct, era_home, era_away,
                        home_field_edge_logodds=HOME_FIELD_EDGE,
                        pitcher_era_scale=PITCHER_ERA_SCALE, pitcher_edge_cap=PITCHER_EDGE_CAP):
    """
    log5(home_pct, away_pct) -> + localía (en espacio de log-odds, ver
    AUDITORÍA 13/09/2026 en HOME_FIELD_EDGE) -> + pitcher_edge (AHORA
    también en espacio de log-odds, ver AUDITORÍA 17/09/2026 abajo) ->
    cap de sanidad 0.05-0.95.

    AUDITORÍA (17/09/2026, a partir de la calibración por bucket del
    dashboard en la versión e5b399eb -- 40-50% predicho, n=16, solo 25%
    de acierto real): pitcher_edge seguía sumándose directo a una
    probabilidad (raw_prob = prob_with_home_field + edge), el MISMO tipo
    de distorsión que HOME_FIELD_EDGE tenía antes del 13/09 -- empuja
    desproporcionadamente fuerte cuando el partido YA está lejos de 50-50
    (que es exactamente la situación de una apuesta a desvalido: log5+
    localía ya dio algo bajo, y ahí pitcher_edge todavía sumaba hasta
    ±0.08 en probabilidad cruda, en vez de un delta más chico una vez
    lejos del centro de la curva). La auditoría del 12/09/2026 ya había
    aislado a pitcher_edge como el mayor contribuyente individual al
    exceso de confianza (Brier 0.388 -> 0.298 al sacarlo, sobre las 22
    señales con componentes guardados) -- por simetría, el mismo mecanismo
    puede estar inflando probabilidades de desvalido, no solo de favorito.
    Se aplica ahora la misma solución que ya funcionó para la localía:
    pitcher_edge se sigue calculando igual (mismos PITCHER_ERA_SCALE/
    PITCHER_EDGE_CAP, mismo significado "shift desde un 50-50" -- ver
    pitcher_edge() y _prob_shift_to_log_odds()), pero se COMPONE en
    log-odds junto con log5+localía en vez de sumarse aparte en
    probabilidad. Pendiente confirmar con un backtest fresco (no se puede
    correr desde este entorno de desarrollo, sin acceso a statsapi.mlb.com
    -- ver TODO en el header del archivo) si esto mejora el Brier
    específicamente en el bucket 40-50%, y volver a fitear
    CALIBRATION_A/CALIBRATION_B con mlb_calibration.py sobre ese backtest
    (la curva actual, a=0.4018/b=0.0525, se fiteó sobre el raw_prob VIEJO
    -- con la fórmula nueva el raw_prob cambia de distribución y la curva
    queda desactualizada hasta refitear).

    NUEVO (13/09/2026): extraído de estimate_win_probability() para que
    backtest_mlb.py pueda llamar exactamente esta misma función en vez de
    reimplementar la secuencia a mano -- antes el backtest duplicaba esta
    matemática (log5 -> +home_field_edge -> +pitcher_edge -> cap) por
    separado, con el riesgo de irse desincronizando de este módulo la
    próxima vez que alguien toque una constante acá y se olvide del otro
    archivo. Con esto, un solo lugar de verdad para ambos.

    Devuelve (raw_prob sin el cap 0.05-0.95, prob ya con el cap, edge de
    pitcher ya aplicado, EN UNIDADES DE PROBABILIDAD -- ver AUDITORÍA
    17/09/2026 arriba: el shape/las unidades de este componente en
    components["pitcher_edge"] no cambian aunque ahora se componga en
    log-odds internamente, para no romper el dashboard ni requerir
    migración) -- el shape que generate_mlb_signal()/backtest_mlb.py
    necesitan para guardar raw_my_prob/my_prob y el componente pitcher_edge
    por separado.
    """
    base_prob = log5(home_pct, away_pct)
    edge = pitcher_edge(era_home, era_away, scale=pitcher_era_scale, cap=pitcher_edge_cap)
    combined_log_odds = (
        to_log_odds(base_prob) + home_field_edge_logodds + _prob_shift_to_log_odds(edge)
    )
    raw_prob = from_log_odds(combined_log_odds)
    prob = max(0.05, min(0.95, raw_prob))
    return raw_prob, prob, edge


def estimate_win_probability(home_id, away_id, home_pitcher_id, away_pitcher_id, season,
                              home_field_edge=HOME_FIELD_EDGE):
    """
    Probabilidad de que el equipo LOCAL gane. Devuelve (prob_home, notes,
    confidence_penalty, components) -- mismo shape que estimate_adjusted_high()
    en weather_signal_engine.py para los primeros tres: penalty sube con
    cada fuente de dato faltante, para que generate_mlb_signal() pueda
    exigir más EV cuando el modelo tiene menos con qué respaldar la
    estimación.

    AUDITORÍA (07/09/2026): se agrega `components` -- un desglose de cada
    ingrediente (win% de cada lado, ERA de cada probable, el ajuste de
    pitcher ya aplicado, la localía usada) -- porque generate_mlb_signal()
    solo guardaba en mlb_signals el `my_prob` final. Se detectó calibración
    mala justo en 60-80% de confianza (11-50% de aciertos reales contra
    65-75% que decía el modelo) y sin estos componentes por señal no hay
    forma de aislar si el culpable es HOME_FIELD_EDGE o PITCHER_ERA_SCALE
    (ambos siguen "sin calibrar" -- ver auditoría de arriba) o si es ruido
    de muestra chica. Con esto guardado, dentro de un par de semanas se
    puede correlacionar cada componente contra el outcome real.
    """
    notes = []
    penalty = 0.0
    era_home = era_away = None

    form_home = fetch_team_form(home_id, season)
    form_away = fetch_team_form(away_id, season)
    home_pct, home_missing = blended_win_pct(form_home)
    away_pct, away_missing = blended_win_pct(form_away)
    if home_missing or away_missing:
        penalty += 0.3
        notes.append(
            "Sin forma de equipo confiable para uno de los dos lados (sin datos, o con menos de "
            f"{MIN_GAMES_FOR_FORM} partidos jugados en la temporada -- ver AUDITORÍA 13/09/2026) "
            "-- se usó 50% neutral."
        )
    notes.append(f"log5 win%: local {home_pct:.3f} vs. visita {away_pct:.3f}")
    notes.append(f"+ localía (log-odds): {home_field_edge:+.3f}")

    if not home_pitcher_id or not away_pitcher_id:
        penalty += 0.2
        notes.append("Falta pitcher probable confirmado de al menos un lado.")
    else:
        # AUDITORÍA (13/09/2026): FIP en vez de ERA -- ver FIP_CONSTANT
        # arriba. era_home/era_away se mantienen como nombres de variable
        # (y como nombres de columna en Supabase/mlb_signals, para no
        # requerir una migración) pero desde ahora contienen FIP, no ERA.
        era_home = fetch_pitcher_fip(home_pitcher_id, season)
        era_away = fetch_pitcher_fip(away_pitcher_id, season)
        if era_home is None or era_away is None:
            penalty += 0.15
            notes.append("FIP de temporada insuficiente (pocas entradas) para uno de los dos probables.")

    # Ver combine_components() -- misma función que usa backtest_mlb.py, para
    # que la matemática de log5 + localía (log-odds) + pitcher_edge + cap de
    # sanidad viva en un solo lugar.
    raw_prob, prob, edge = combine_components(home_pct, away_pct, era_home, era_away,
                                               home_field_edge_logodds=home_field_edge)
    if era_home is not None and era_away is not None:
        notes.append(f"+ pitchers (FIP {era_home:.2f} vs {era_away:.2f}): {edge:+.3f}")

    components = {
        "home_win_pct": round(home_pct, 3),
        "away_win_pct": round(away_pct, 3),
        "era_home": round(era_home, 2) if era_home is not None else None,
        "era_away": round(era_away, 2) if era_away is not None else None,
        "pitcher_edge": round(edge, 3),
        "home_field_edge": round(home_field_edge, 3),
    }
    return round(prob, 3), notes, round(min(penalty, 1.0), 2), components


def price_disagrees_with_model(direction_is_yes, momentum_data, threshold=MOMENTUM_DISAGREEMENT_THRESHOLD):
    """
    True si el precio viene moviéndose con fuerza EN CONTRA del lado que
    favorece el modelo de fundamentos, en las últimas velas (ver
    analyze_probability_momentum, importada del motor genérico).

    No se usa para elegir dirección ni para sumar score -- eso ya lo
    decide el modelo de fundamentos. Se usa como alerta: la lectura más
    probable de un movimiento fuerte en contra no es "el mercado se
    equivoca", es "el mercado ya sabe algo que este modelo todavía no"
    (pitcher escrachado a último momento, lineup con bajas, lluvia que
    atrasa el partido) -- el pitcher probable puede cambiar hasta minutos
    antes y este motor solo se entera si vuelve a consultar /schedule.
    """
    if not momentum_data:
        return False
    momentum = momentum_data["momentum"]
    return momentum < -threshold if direction_is_yes else momentum > threshold


def generate_mlb_signal(market, min_ev=0.05, season=None, today_games=None, price_history=None):
    """
    Punto de entrada equivalente a generate_weather_signal(): recibe un
    mercado ya parseado por parse_market_for_analysis() (necesita
    question, yes_label, no_label, yes_price, condition_id, yes_token_id),
    resuelve si es un mercado moneyline de MLB reconocible, arma la
    probabilidad de fundamentos y la compara contra el precio real.
    Devuelve None si no aplica (no es de MLB, no hay partido hoy entre esos
    dos equipos, o el EV no llega al mínimo) -- mismo contrato que
    generate_polymarket_signal().

    `today_games`: pasar el resultado de fetch_probable_pitchers_for_date()
    UNA vez por ciclo (no por mercado) para no pegarle N veces al mismo
    endpoint de /schedule.
    """
    team_yes = resolve_team_id(market.get("yes_label"))
    team_no = resolve_team_id(market.get("no_label"))
    if not team_yes or not team_no or team_yes == team_no:
        return None

    if today_games is None:
        today_games = fetch_probable_pitchers_for_date(current_mlb_date())

    game = next(
        (g for g in today_games if {g["home_id"], g["away_id"]} == {team_yes, team_no}),
        None,
    )
    if not game:
        return None  # no hay partido HOY entre estos dos equipos

    home_name = TEAMS[game["home_id"]][0]
    away_name = TEAMS[game["away_id"]][0]
    if not is_moneyline_question(market.get("question"), game["home_id"], game["away_id"]):
        return None  # mercado derivado (spread/total) del mismo partido, no el moneyline real

    # AUDITORÍA (06/09/2026, usuario reportó "RETORNO PROMEDIO +8315.5%" sin
    # lógica en el dashboard): confirmado en producción -- una vez que un
    # partido termina y su señal se resuelve, el game_pk queda libre en el
    # dedupe de open_game_pks (correcto, ya no hay señal ABIERTA para ese
    # partido), pero el mercado de Polymarket sigue técnicamente operable
    # un rato más mientras liquida. Si el ciclo vuelve a correr en esa
    # ventana, generate_mlb_signal() no tenía forma de saber que el
    # partido YA se jugó -- seguía comparando el precio (ya desplomado a
    # ~0 o ~1 porque el mercado real SÍ sabe el resultado) contra una
    # probabilidad de fundamentos calculada como si el partido fuera
    # incierto, generando una "señal" fantasma con edge artificialmente
    # gigante. Ejemplo real: Diamondbacks @ Astros, generada a las 03:00
    # del día siguiente (el partido ya había terminado horas antes),
    # market_price=0.0005 -- esa fila sola generó un retorno simulado de
    # +199.900% (turn (1-0.0005)/0.0005) que, promediado sobre 24 señales
    # resueltas, es justamente el +8315.5% que se ve en el dashboard.
    # Se corta de raíz consultando si el partido ya está Final antes de
    # seguir.
    if fetch_game_result(game["game_pk"]) is not None:
        return None  # el partido real ya terminó -- el mercado quedó desactualizado/en liquidación, no es una oportunidad real

    season = season or current_mlb_date()[:4]
    prob_home, notes, penalty, components = estimate_win_probability(
        game["home_id"], game["away_id"], game["home_pitcher_id"], game["away_pitcher_id"], season,
    )

    # AUDITORÍA (13/09/2026): se reemplaza el clip fijo [0.40,0.60] por
    # calibrate_prob() -- ver AUDITORÍA larga junto a CALIBRATION_A/B
    # arriba. Se hace acá, apenas sale del modelo de fundamentos, para que
    # TODO lo que se deriva después (EV, confidence, ½ Kelly vía my_prob)
    # ya use el valor calibrado -- ningún cálculo río abajo debe ver la
    # probabilidad cruda sin calibrar. Se mantiene el mismo cap de sanidad
    # 0.05-0.95 después de calibrar (defensa en profundidad -- aunque
    # calibrate_prob() ya es más conservador que el crudo cerca de los
    # extremos por construcción, con pendiente<1).
    raw_prob_home = prob_home
    prob_home = max(0.05, min(0.95, calibrate_prob(prob_home)))
    notes.append(
        f"my_prob calibrado de {raw_prob_home*100:.1f}% a {prob_home*100:.1f}% "
        f"(Platt fiteado sobre backtest 2023-2025, ver mlb_calibration.py -- reemplaza al clip 09/09/2026)."
    )

    my_prob_yes = prob_home if team_yes == game["home_id"] else round(1 - prob_home, 3)
    raw_my_prob_yes = raw_prob_home if team_yes == game["home_id"] else round(1 - raw_prob_home, 3)

    yes_price = market.get("yes_price")
    no_price = market.get("no_price")
    if yes_price is None:
        return None

    # Blindaje adicional (independiente del chequeo de partido terminado de
    # arriba, por si el status "Final" de la MLB Stats API todavía no
    # propagó o el partido está suspendido en un estado raro): un precio
    # ya en el extremo significa que el mercado real ya está prácticamente
    # decidido, sea por qué sea -- no hay edge real que capturar ahí, solo
    # ruido de un mercado en vías de liquidación.
    #
    # AUDITORÍA (10/09/2026): el piso vivía hardcodeado acá en 0.02 y no
    # alcanzaba -- ver auditoría larga en Config.MLB_EXTREME_PRICE_FLOOR
    # (config.py) para el caso real que lo disparó (Mets @ Marlins,
    # market_price=$0.024, retorno simulado +4067%) y el dato que
    # justifica subirlo a 0.10 (7 de 8 señales resueltas en la franja
    # <10c/>90c, con el único fallo sugiriendo que ni siquiera es de baja
    # varianza real). Se movió a Config para que sea ajustable por env var
    # sin tocar código, igual que MLB_PROB_CLIP_MIN/MAX.
    if yes_price <= Config.MLB_EXTREME_PRICE_FLOOR or yes_price >= (1 - Config.MLB_EXTREME_PRICE_FLOOR):
        return None

    # Evaluar los dos lados y quedarse con el de mejor EV -- el edge puede
    # estar en cualquiera de los dos equipos, no siempre en el que quedó
    # como "yes_label" en Polymarket.
    ev_yes = compute_ev(my_prob_yes, yes_price)
    ev_no = compute_ev(round(1 - my_prob_yes, 3), no_price) if no_price is not None else None

    if ev_no is not None and (ev_yes is None or ev_no > ev_yes):
        direction_is_yes, my_prob, price = False, round(1 - my_prob_yes, 3), no_price
        raw_my_prob = round(1 - raw_my_prob_yes, 3)
        token_id = market.get("no_token_id")
    else:
        direction_is_yes, my_prob, price = True, my_prob_yes, yes_price
        raw_my_prob = raw_my_prob_yes
        token_id = market.get("yes_token_id")
    ev = ev_no if not direction_is_yes else ev_yes
    if ev is None:
        return None

    # Guardas de riesgo tomadas del motor genérico (polymarket_signal_engine.py)
    # -- no reemplazan el modelo de fundamentos, solo lo hacen más exigente
    # cuando hay señales de que el precio sabe algo que el modelo no.
    effective_min_ev = min_ev
    momentum_data = analyze_probability_momentum(price_history) if price_history else None
    if price_disagrees_with_model(direction_is_yes, momentum_data):
        effective_min_ev = max(min_ev * 2, min_ev + 0.15)
        notes.append(
            f"Precio moviéndose fuerte en contra del lado del modelo "
            f"(momentum {momentum_data['momentum'] * 100:+.1f}% en las últimas velas) "
            f"-- posible pitcher/lineup nuevo que el modelo no tiene. Se exige el doble de EV."
        )

    if no_price is not None:
        inefficiency = detect_inefficiency({"yes_price": yes_price, "no_price": no_price})
        if inefficiency["is_extreme_trap"]:
            effective_min_ev = max(effective_min_ev, min_ev * 2)
            notes.append(f"Precio extremo ({price:.2f}) -- riesgo de trampa de liquidez, libro puede estar fino.")

    if ev < effective_min_ev:
        return None

    confidence = max(1, min(5, round((1 - penalty) * 5)))

    return {
        "status": "ok",
        "type": "MLB_SIGNAL",
        "condition_id": market.get("condition_id"),
        "game_pk": game["game_pk"],
        "question": market.get("question"),
        "home_team": home_name,
        "away_team": away_name,
        "home_pitcher_id": game["home_pitcher_id"],
        "away_pitcher_id": game["away_pitcher_id"],
        "direction": "YES" if direction_is_yes else "NO",
        "my_prob": my_prob,
        # NUEVO (09/09/2026), actualizado (13/09/2026): probabilidad SIN
        # calibrar por calibrate_prob() -- ver AUDITORÍA junto a
        # CALIBRATION_A/B arriba (antes esto era "sin recortar por el clip
        # fijo", pero el clip fijo ya no se usa acá, se reemplazó por la
        # calibración). Guardar la cruda en paralelo sigue sirviendo para
        # lo mismo que el 09/09: seguir juntando muestra para volver a
        # correr mlb_calibration.py más adelante con más datos y confirmar
        # que a/b no se corrieron.
        "raw_my_prob": raw_my_prob,
        # NUEVO (13/09/2026): ver AUDITORÍA junto a MODEL_VERSION arriba --
        # fingerprint de las constantes activas al momento de generar esta
        # señal puntual, para poder agrupar/calibrar por versión en el
        # dashboard sin cutoffs hardcodeados.
        "model_version": MODEL_VERSION,
        "market_price": price,
        "ev": ev,
        "min_ev_threshold": effective_min_ev,
        "confidence_penalty": penalty,
        "confidence": confidence,
        # AUDITORÍA (07/09/2026): desglose de estimate_win_probability() para
        # poder auditar calibración por componente -- ver AUDITORÍA en esa
        # función. home_win_pct/away_win_pct/era_home/era_away/pitcher_edge
        # son siempre del equipo LOCAL/VISITA tal cual (no del lado
        # comprado), home_field_edge es el ajuste de localía usado.
        **components,
        # AUDITORÍA (05/09/2026, categoría 6): parse_market_for_analysis()
        # en polymarket_client.py ya arma este link (vía _build_market_url,
        # slug del evento) y lo deja en market["url"] -- acá se descartaba
        # sin usarlo, así que build_mlb_memo() nunca podía mostrarlo.
        "url": market.get("url"),
        "notes": notes,
        "token_id": token_id,
    }


def build_mlb_memo(signal, markdown=True):
    """
    Mensaje de Telegram para una señal de MLB. Mismo formato que
    build_weather_memo() en weather_signal_engine.py -- decisión explícita
    primero (qué comprar y de qué equipo), edge en puntos porcentuales,
    EV, tamaño sugerido por ½ Kelly, link directo al mercado, y las notas
    del modelo (log5, localía, pitchers, alertas de momentum/liquidez si
    dispararon) como sección aparte al final.

    AUDITORÍA (05/09/2026): dos problemas del formato anterior:
    1) Nunca incluía el link al mercado en Polymarket -- generate_mlb_signal()
       descartaba market["url"] (ya parseado por parse_market_for_analysis(),
       ver categoría 6) en vez de propagarlo. Sin el fix de ese lado, acá no
       había nada que mostrar.
    2) Todo el bloque de números (prob/mercado/edge/EV/umbral) iba en una
       sola línea larga -- legible en desktop, pero en el celular (donde de
       hecho se lee esto, es un bot de Telegram) se corta o se ve apretado.
       Se separa en líneas cortas con su propia etiqueta, y las notas del
       modelo pasan a una sección con su propio header ("📐 Cómo se armó la
       probabilidad") en vez de bullets sueltos pegados abajo sin contexto.
    """
    if not signal or signal.get("status") != "ok":
        return None

    side_team = signal["home_team"] if signal["direction"] == "YES" else signal["away_team"]
    edge_pp = (signal["my_prob"] - signal["market_price"]) * 100
    title_txt = f"{signal['away_team']} @ {signal['home_team']}"[:70]

    lines = [
        f"⚾ *ANÁLISIS DE MLB* — {title_txt}" if markdown else f"⚾ ANÁLISIS DE MLB — {title_txt}",
        "",
    ]
    label = f"SEÑAL: COMPRAR \"{signal['direction']}\" — {side_team}"
    lines.append(f"🟢 *{label}*" if markdown else f"🟢 {label}")
    lines.append("")
    lines.append(f"   Mi prob: {signal['my_prob']*100:.0f}%  ·  Mercado: {signal['market_price']*100:.1f}¢")
    lines.append(f"   Edge: {edge_pp:+.0f}pp  ·  EV: {signal['ev']*100:+.0f}% (mínimo exigido: {signal['min_ev_threshold']*100:.0f}%)")
    kelly = _half_kelly_fraction(signal["my_prob"], signal["market_price"], max_pct=Config.MAX_KELLY_STAKE_PCT)
    if kelly is not None:
        capped_note = " (con techo)" if kelly >= Config.MAX_KELLY_STAKE_PCT else ""
        lines.append(f"   Tamaño sugerido (½ Kelly, informativo){capped_note}: {kelly*100:.1f}% del bankroll")
    lines.append(f"   Confianza: {signal['confidence']}/5 (penalty {signal['confidence_penalty']:.2f})")

    if signal.get("url"):
        lines.append("")
        link_text = "Ver en Polymarket"
        lines.append(f"🔗 [{link_text}]({signal['url']})" if markdown else signal["url"])

    if signal.get("notes"):
        lines.append("")
        lines.append("📐 *Cómo se armó la probabilidad:*" if markdown else "📐 Cómo se armó la probabilidad:")
        for note in signal["notes"]:
            lines.append(f"   • {note}")

    return "\n".join(lines)
