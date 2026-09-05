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
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from weather_signal_engine import compute_ev, _half_kelly_fraction
from polymarket_signal_engine import analyze_probability_momentum, detect_inefficiency

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

HOME_FIELD_EDGE = 0.04       # ver AUDITORÍA arriba -- sin calibrar
PITCHER_ERA_SCALE = 0.10     # cuánta prob. mueve 1.0 de diferencia de ERA -- sin calibrar
SEASON_FORM_WEIGHT = 0.7     # peso de win% de temporada vs. últimos-10 en blended_win_pct
MIN_INNINGS_FOR_ERA = 15.0   # por debajo de esto, ERA de pocas salidas es ruido
MOMENTUM_DISAGREEMENT_THRESHOLD = 0.08  # ver price_disagrees_with_model() -- sin calibrar

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
                return {
                    "win_pct": float(team_record.get("winningPercentage", 0.5) or 0.5),
                    "last_ten_pct": last_ten_pct,
                }
    return None


def fetch_pitcher_era(person_id, season):
    """ERA de temporada de un pitcher. None si no encuentra el stat o si
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


def blended_win_pct(form, season_weight=SEASON_FORM_WEIGHT):
    """Mezcla win% de temporada con la forma de los últimos 10. Sin datos
    de últimos 10 (arranque de temporada), usa solo el de temporada. Sin
    ningún dato, devuelve 50% neutral y lo marca como faltante."""
    if form is None:
        return 0.5, True
    if form["last_ten_pct"] is None:
        return form["win_pct"], False
    return season_weight * form["win_pct"] + (1 - season_weight) * form["last_ten_pct"], False


def log5(pct_a, pct_b):
    """Fórmula de Bill James: prob. de que A le gane a B dados sus win%
    "verdaderos", sin ajuste de local ni de pitcher todavía."""
    denom = pct_a + pct_b - 2 * pct_a * pct_b
    if denom <= 0:
        return 0.5
    return (pct_a - pct_a * pct_b) / denom


def pitcher_edge(era_a, era_b, scale=PITCHER_ERA_SCALE):
    """Diferencia de ERA entre los dos probables -> ajuste de probabilidad
    a favor de A. Positivo si A tiene mejor (más bajo) ERA. Cap a ±0.15
    para que un mismatch de ERA extremo no domine por sí solo toda la
    estimación -- mismo espíritu que el cap de sanidad al final de
    estimate_win_probability."""
    if era_a is None or era_b is None:
        return 0.0
    return max(-0.15, min(0.15, (era_b - era_a) * scale))


def estimate_win_probability(home_id, away_id, home_pitcher_id, away_pitcher_id, season,
                              home_field_edge=HOME_FIELD_EDGE):
    """
    Probabilidad de que el equipo LOCAL gane. Devuelve (prob_home, notes,
    confidence_penalty) -- mismo shape que estimate_adjusted_high() en
    weather_signal_engine.py: penalty sube con cada fuente de dato
    faltante, para que generate_mlb_signal() pueda exigir más EV cuando el
    modelo tiene menos con qué respaldar la estimación.
    """
    notes = []
    penalty = 0.0

    home_pct, home_missing = blended_win_pct(fetch_team_form(home_id, season))
    away_pct, away_missing = blended_win_pct(fetch_team_form(away_id, season))
    if home_missing or away_missing:
        penalty += 0.3
        notes.append("Sin forma de equipo para uno de los dos lados -- se usó 50% neutral.")

    prob = log5(home_pct, away_pct)
    notes.append(f"log5 win%: local {home_pct:.3f} vs. visita {away_pct:.3f} -> {prob:.3f}")

    prob += home_field_edge
    notes.append(f"+ localía: {home_field_edge:+.3f}")

    if not home_pitcher_id or not away_pitcher_id:
        penalty += 0.2
        notes.append("Falta pitcher probable confirmado de al menos un lado.")
    else:
        era_home = fetch_pitcher_era(home_pitcher_id, season)
        era_away = fetch_pitcher_era(away_pitcher_id, season)
        if era_home is None or era_away is None:
            penalty += 0.15
            notes.append("ERA de temporada insuficiente (pocas entradas) para uno de los dos probables.")
        else:
            edge = pitcher_edge(era_home, era_away)
            prob += edge
            notes.append(f"+ pitchers (ERA {era_home:.2f} vs {era_away:.2f}): {edge:+.3f}")

    # Cap de sanidad -- igual que build_bucket_distribution en el motor de
    # clima: nunca dejar que la probabilidad final sugiera una certeza que
    # el modelo no tiene fundamento real para respaldar.
    prob = max(0.05, min(0.95, prob))
    return round(prob, 3), notes, round(min(penalty, 1.0), 2)


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

    season = season or current_mlb_date()[:4]
    prob_home, notes, penalty = estimate_win_probability(
        game["home_id"], game["away_id"], game["home_pitcher_id"], game["away_pitcher_id"], season,
    )
    my_prob_yes = prob_home if team_yes == game["home_id"] else round(1 - prob_home, 3)

    yes_price = market.get("yes_price")
    no_price = market.get("no_price")
    if yes_price is None:
        return None

    # Evaluar los dos lados y quedarse con el de mejor EV -- el edge puede
    # estar en cualquiera de los dos equipos, no siempre en el que quedó
    # como "yes_label" en Polymarket.
    ev_yes = compute_ev(my_prob_yes, yes_price)
    ev_no = compute_ev(round(1 - my_prob_yes, 3), no_price) if no_price is not None else None

    if ev_no is not None and (ev_yes is None or ev_no > ev_yes):
        direction_is_yes, my_prob, price = False, round(1 - my_prob_yes, 3), no_price
        token_id = market.get("no_token_id")
    else:
        direction_is_yes, my_prob, price = True, my_prob_yes, yes_price
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
        "home_team": TEAMS[game["home_id"]][0],
        "away_team": TEAMS[game["away_id"]][0],
        "home_pitcher_id": game["home_pitcher_id"],
        "away_pitcher_id": game["away_pitcher_id"],
        "direction": "YES" if direction_is_yes else "NO",
        "my_prob": my_prob,
        "market_price": price,
        "ev": ev,
        "min_ev_threshold": effective_min_ev,
        "confidence_penalty": penalty,
        "confidence": confidence,
        "notes": notes,
        "token_id": token_id,
    }


def build_mlb_memo(signal, markdown=True):
    """
    Mensaje de Telegram para una señal de MLB. Mismo formato que
    build_weather_memo() en weather_signal_engine.py -- decisión explícita
    primero (qué comprar y de qué equipo), edge en puntos porcentuales,
    EV, tamaño sugerido por ½ Kelly, y las notas del modelo (log5,
    localía, pitchers, alertas de momentum/liquidez si dispararon).
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
    lines.append(
        f"   Mi prob: {signal['my_prob']*100:.0f}% | Mercado: {signal['market_price']*100:.1f}¢ | "
        f"Edge: {edge_pp:+.0f}pp | EV: {signal['ev']*100:+.0f}% (mínimo exigido: {signal['min_ev_threshold']*100:.0f}%)"
    )
    kelly = _half_kelly_fraction(signal["my_prob"], signal["market_price"])
    if kelly is not None:
        lines.append(f"   Tamaño sugerido (½ Kelly, informativo): {kelly*100:.1f}% del bankroll")
    lines.append(f"   Confianza: {signal['confidence']}/5 (penalty {signal['confidence_penalty']:.2f})")
    lines.append("")
    for note in signal.get("notes", []):
        lines.append(f"   • {note}")

    return "\n".join(lines)
