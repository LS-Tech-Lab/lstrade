"""
Backtest histórico de estimate_win_probability() (mlb_signal_engine.py) sobre
temporadas completas de MLB ya jugadas, vía MLB Stats API (statsapi.mlb.com --
pública, sin API key).

AUDITORÍA (13/09/2026, pedido del usuario -- "no hay backtest histórico de
MLB, todo el ajuste se está haciendo a ciegas sobre 20-40 señales en vivo"):
a diferencia de backtest.py (cripto, OHLCV de un exchange) y
polymarket_backtest.py, no existía ningún backtest para MLB -- cada
constante (HOME_FIELD_EDGE, PITCHER_ERA_SCALE, SEASON_FORM_WEIGHT) se viene
ajustando esperando semana a semana que caigan un puñado más de señales
reales de Polymarket, con todo el ruido que eso implica (ver AUDITORÍA
12/09/2026 en mlb_signal_engine.py: un bucket de n=5 dio "100% de acierto").
La MLB Stats API tiene resultados y stats de temporadas completas gratis --
esto arma un dataset de miles de partidos (2023-2025 por defecto) para
correr el mismo modelo contra órdenes de magnitud más de datos.

AUDITORÍA (13/09/2026, tercera pasada -- "Mejoras al modelo en sí" del
pedido original): se agregan FIP (reemplaza a ERA en el pitcher_edge, ver
FIP_CONSTANT en mlb_signal_engine.py) y Pythagorean win expectancy
(reemplaza al win% crudo de TEMPORADA en blended_win_pct, no al de
últimos-10) al mismo backtest, reconstruidos igual de cronológicos y sin
lookahead que todo lo demás acá: FIP necesita HR/BB/HBP/K acumulados por
pitcher antes de la fecha (mismo gameLog que ya se pedía para ERA, solo se
piden más campos), y Pythagorean necesita carreras anotadas/permitidas
acumuladas por equipo antes de la fecha (se sacan del propio /schedule,
que ya trae el resultado final de cada partido vía linescore -- no hace
falta un fetch nuevo). Bullpen y factores de parque, los otros dos puntos
del pedido, se evalúan y quedan deliberadamente sin implementar -- ver
AUDITORÍA junto a PYTHAGOREAN_EXPONENT en mlb_signal_engine.py para el
motivo de cada uno (factores de parque: las fuentes públicas discrepan
demasiado entre sí para hardcodear una tabla con confianza; bullpen: no
hay forma de confirmar el endpoint/campo correcto de la MLB Stats API sin
acceso real para probarlo).

DECISIÓN DE DISEÑO CRÍTICA -- sin lookahead bias: estimate_win_probability()
en producción usa win%/ERA de temporada consultados EN VIVO, que en el
momento de cada señal real solo reflejan partidos ya jugados. Si este
backtest usara el win% FINAL de temporada o el ERA final del pitcher para
evaluar un partido de abril, estaría usando información del futuro (incluido
el resultado del propio partido que se está evaluando) -- el backtest daría
una edge falsa que no existió en tiempo real. Por eso:
  - El win%/últimos-10 de cada equipo se reconstruye partido a partido en
    orden cronológico (ver _build_team_form_asof): para el partido de un
    equipo en la fecha D, se usa solo su récord ANTES de D.
  - El ERA del pitcher probable se reconstruye igual desde su gameLog
    (stats=gameLog): IP y carreras limpias acumuladas ANTES de la fecha D,
    con el mismo MIN_INNINGS_FOR_ERA importado de mlb_signal_engine (no
    duplicado) para exigir la misma muestra mínima que en producción.
Todas las constantes del modelo (HOME_FIELD_EDGE, PITCHER_ERA_SCALE,
PITCHER_EDGE_CAP, SEASON_FORM_WEIGHT, MIN_INNINGS_FOR_ERA, MIN_GAMES_FOR_FORM)
y las funciones puras (blended_win_pct, combine_components -- que a su vez
usa log5/pitcher_edge/to_log_odds/from_log_odds internamente) se IMPORTAN de
mlb_signal_engine.py en vez de reimplementarse -- mismo motivo que
model_version (13/09/2026, ese mismo archivo): un solo lugar de verdad para
la matemática del modelo, así que tocar una constante (o la fórmula misma)
ahí se refleja acá sin tener que mantener una copia sincronizada a mano.
AUDITORÍA (13/09/2026, mismo día -- primer backtest real corrido y
analizado): la primera versión de este script SÍ duplicaba la secuencia
log5 -> +home_field_edge -> +pitcher_edge -> cap a mano en
compute_prob_home(). Con los resultados de esa corrida (7303 partidos,
Brier ~0.26, sobreconfianza fuerte en ambos extremos de la calibración) se
identificaron dos causas y se corrigieron en mlb_signal_engine.py: (1)
HOME_FIELD_EDGE pasó a espacio de log-odds (antes se sumaba directo a una
probabilidad, empujando desproporcionadamente cerca de 0.05/0.95), (2) se
agregó MIN_GAMES_FOR_FORM=15 (el bucket de probabilidad más baja estaba
dominado por partidos de arranque de temporada con win% de 0-3 partidos
jugados). Se aprovechó el cambio para extraer la secuencia completa a
combine_components() en mlb_signal_engine.py, así este script ya no puede
volver a desincronizarse de la matemática real.

CAVEAT sin poder validar contra la API real (ver TODO en el header de
mlb_signal_engine.py -- statsapi.mlb.com no es alcanzable desde este entorno
de desarrollo): los nombres de campo de /schedule, /people/{id}/stats
(stats=gameLog) y el formato "X.Y" de inningsPitched (Y = tercios de
entrada, NO decimal -- "6.2" es 6 y 2/3, no 6.2) están tomados de
documentación de terceros (pseudo-r/Public-MLB-API) y de cómo MLB reporta
estos números públicamente, no de una respuesta real inspeccionada acá.
Correr con --seasons de una sola temporada chica primero y revisar
manualmente un puñado de filas del --output antes de confiar en un run
grande.

CAVEAT sobre "pitcher probable" histórico: hydrate=probablePitcher en
/schedule para fechas pasadas devuelve quién era el probable ANTES del
partido (no necesariamente quien terminó abriendo si hubo un scratch de
último momento) -- es la misma fuente que usa el motor en vivo
(fetch_probable_pitchers_for_date), así que el backtest mide el modelo tal
cual se comporta con el dato que realmente tiene disponible, pero puede
haber un puñado de casos donde el "probable" difiere del abridor real.

USO DE LA MLB STATS API -- a diferencia de mlb_signal_engine.py (que según
su propio header solo consulta el partido puntual que ya matcheó una señal,
"no hace scraping en bulk de temporadas completas"), ESTE script sí hace
fetch en bulk de temporadas completas a propósito, para backtesting -- no
para producción ni para correr en cada ciclo. Se cachea todo en disco
(--cache-dir, default .backtest_cache/mlb/) para no tener que repetir estas
~150-200 llamadas de gameLog por pitcher/temporada en cada corrida (por
ejemplo al iterar --sweep varias veces), y se duerme un rato corto entre
llamadas -- ver DEFAULT_SLEEP_SECONDS.

Uso:
    python backtest_mlb.py --seasons 2023,2024,2025
    python backtest_mlb.py --seasons 2024 --output resultados_mlb_2024.csv
    python backtest_mlb.py --seasons 2023,2024,2025 --sweep
    python backtest_mlb.py --seasons 2023,2024,2025 --oos-frac 0.3
"""
import argparse
import csv
import json
import logging
import os
import time
from datetime import date, timedelta

from mlb_signal_engine import (
    _get,
    _parse_innings_pitched,
    HOME_FIELD_EDGE,
    PITCHER_ERA_SCALE,
    PITCHER_EDGE_CAP,
    SEASON_FORM_WEIGHT,
    MIN_INNINGS_FOR_ERA,
    MIN_GAMES_FOR_FORM,
    FIP_CONSTANT,
    PYTHAGOREAN_EXPONENT,
    TEAMS,
    blended_win_pct,
    combine_components,
    pythagorean_win_pct,
)
from config import Config

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("backtest_mlb")

DEFAULT_SLEEP_SECONDS = 0.15  # cortesía con la API pública -- ver AUDITORÍA de arriba
DEFAULT_CACHE_DIR = ".backtest_cache/mlb"

# AUDITORÍA (14/09/2026, primer crash real en producción -- KeyError:
# 'home_score' en build_team_form_asof): el caché en disco se guardaba con
# una key que NO incluía versión de schema (solo f"schedule_{season}" /
# f"pitcher_{pid}_{season}"). Cuando se agregaron home_score/away_score al
# schedule (para Pythagorean) y hr/bb/hbp/so al gameLog (para FIP), una
# corrida que reusaba caché de ANTES de esos cambios cargaba JSON sin esos
# campos y el código nuevo explotaba al primer acceso. Se sube
# CACHE_SCHEMA_VERSION cada vez que cambie la FORMA de lo que se cachea
# (qué claves tiene cada dict) -- no hace falta que nadie se acuerde de
# limpiar .backtest_cache/mlb a mano la próxima vez, un caché de schema
# viejo simplemente no matchea la key nueva y se vuelve a descargar solo.
CACHE_SCHEMA_VERSION = 2


# --------------------------------------------------------------------------
# Caché en disco -- JSON plano, una llave por (tipo, temporada[, pitcher_id])
# --------------------------------------------------------------------------

def _cache_path(cache_dir, key):
    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, f"{key}.json")


def _cache_load(cache_dir, key):
    path = _cache_path(cache_dir, key)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _cache_save(cache_dir, key, data):
    path = _cache_path(cache_dir, key)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


# --------------------------------------------------------------------------
# Descarga del schedule de una temporada completa (partidos terminados, con
# pitcher probable de cada lado)
# --------------------------------------------------------------------------

def _month_ranges(season):
    """(inicio, fin) mes a mes de marzo a noviembre -- cubre spring training
    tardío hasta la postemporada, se filtra a regular season (gameType) abajo.
    Se pagina por mes para no pedirle a la API un rango de 9 meses en una
    sola llamada (respuesta más chica, más fácil de reintentar si falla un
    mes puntual sin perder los demás)."""
    ranges = []
    for month in range(3, 12):
        start = date(season, month, 1)
        end = date(season, month + 1, 1) - timedelta(days=1) if month < 12 else date(season, 12, 31)
        ranges.append((start.isoformat(), end.isoformat()))
    return ranges


def fetch_season_games(season, cache_dir, sleep_seconds=DEFAULT_SLEEP_SECONDS):
    """Todos los partidos de temporada regular YA TERMINADOS de una temporada,
    con fecha oficial, equipos, pitcher probable de cada lado y quién ganó.

    Devuelve lista de dicts ordenable cronológicamente:
    {game_pk, date, home_id, away_id, home_pitcher_id, away_pitcher_id, home_won}
    (partidos sin ganador real -- suspendidos/cancelados sin reanudar, ver
    mismo criterio que fetch_game_result en mlb_signal_engine.py -- se
    descartan, no aportan nada a la calibración)."""
    cache_key = f"schedule_{season}_v{CACHE_SCHEMA_VERSION}"
    cached = _cache_load(cache_dir, cache_key)
    if cached is not None:
        log.info(f"{season}: schedule desde caché ({len(cached)} partidos).")
        return cached

    games = []
    for start_str, end_str in _month_ranges(season):
        data = _get("/schedule", {
            "sportId": 1,
            "startDate": start_str,
            "endDate": end_str,
            "gameType": "R",  # solo temporada regular -- ver docstring del módulo
            "hydrate": "probablePitcher,linescore",
        })
        time.sleep(sleep_seconds)
        if not data:
            log.warning(f"{season}: falló /schedule para {start_str}..{end_str}, se salta ese mes.")
            continue
        for d in data.get("dates", []):
            for g in d.get("games", []):
                if g.get("gameType") != "R":
                    continue
                status = g.get("status", {})
                if status.get("abstractGameState") != "Final":
                    continue
                teams = g.get("teams", {})
                home, away = teams.get("home", {}), teams.get("away", {})
                home_won, away_won = bool(home.get("isWinner")), bool(away.get("isWinner"))
                if not home_won and not away_won:
                    continue  # sin ganador real -- ver docstring
                home_id = home.get("team", {}).get("id")
                away_id = away.get("team", {}).get("id")
                if home_id not in TEAMS or away_id not in TEAMS:
                    continue  # equipo de exhibición/no-MLB colado en el rango de fechas
                games.append({
                    "game_pk": g.get("gamePk"),
                    # officialDate (no gameDate/UTC) -- mismo motivo que
                    # current_mlb_date() en mlb_signal_engine.py: es la
                    # fecha de "día de partido" que usa la propia MLB.
                    "date": g.get("officialDate"),
                    "home_id": home_id,
                    "away_id": away_id,
                    "home_pitcher_id": (home.get("probablePitcher") or {}).get("id"),
                    "away_pitcher_id": (away.get("probablePitcher") or {}).get("id"),
                    "home_won": home_won,
                    # AUDITORÍA (13/09/2026): carreras finales de cada lado --
                    # para reconstruir Pythagorean win expectancy
                    # cronológicamente en build_team_form_asof(), sin pedir
                    # un endpoint nuevo (ya viene en este mismo /schedule).
                    "home_score": home.get("score"),
                    "away_score": away.get("score"),
                })

    games.sort(key=lambda g: (g["date"] or "", g["game_pk"]))
    log.info(f"{season}: {len(games)} partidos de temporada regular terminados.")
    _cache_save(cache_dir, cache_key, games)
    return games


# --------------------------------------------------------------------------
# Win% / últimos-10 de cada equipo, reconstruido cronológicamente (sin
# lookahead -- ver AUDITORÍA del header)
# --------------------------------------------------------------------------

def build_team_form_asof(games):
    """Agrega a cada partido (in-place) el win% de temporada, de últimos-10,
    los partidos jugados y el Pythagorean win expectancy de cada equipo
    ANTES de ese partido -- mismo shape que el dict `form` que arma
    fetch_team_form() en mlb_signal_engine.py (None si el equipo no jugó
    ningún partido previo en la temporada todavía), incluyendo games_played
    para MIN_GAMES_FOR_FORM y pyth_win_pct (carreras anotadas/permitidas
    acumuladas, ver AUDITORÍA del header) para que blended_win_pct() lo
    prefiera sobre el win% crudo exactamente igual que en producción."""
    history = {}       # team_id -> lista cronológica de bool (ganó ese partido)
    runs_for = {}       # team_id -> lista cronológica de carreras anotadas
    runs_against = {}   # team_id -> lista cronológica de carreras permitidas

    for g in games:
        for side, team_id, rf_key, ra_key in (
            ("home", g["home_id"], "home_score", "away_score"),
            ("away", g["away_id"], "away_score", "home_score"),
        ):
            past = history.get(team_id, [])
            g[f"{side}_games_played"] = len(past)
            if not past:
                g[f"{side}_win_pct_season"] = None
                g[f"{side}_last10_pct"] = None
                g[f"{side}_pyth_win_pct"] = None
            else:
                g[f"{side}_win_pct_season"] = sum(past) / len(past)
                last10 = past[-10:]
                g[f"{side}_last10_pct"] = sum(last10) / len(last10)
                rf_total = sum(runs_for.get(team_id, []))
                ra_total = sum(runs_against.get(team_id, []))
                g[f"{side}_pyth_win_pct"] = pythagorean_win_pct(rf_total, ra_total)

        history.setdefault(g["home_id"], []).append(g["home_won"])
        history.setdefault(g["away_id"], []).append(not g["home_won"])
        # .get() en vez de indexado directo -- defensa extra además del
        # versionado de caché de arriba (CACHE_SCHEMA_VERSION): si por lo
        # que sea este dict no tiene home_score/away_score (caché de un
        # schema viejo, u otra fuente), se degrada a "sin datos de
        # carreras para Pythagorean" en vez de tirar el proceso entero.
        home_score, away_score = g.get("home_score"), g.get("away_score")
        if home_score is not None and away_score is not None:
            runs_for.setdefault(g["home_id"], []).append(home_score)
            runs_against.setdefault(g["home_id"], []).append(away_score)
            runs_for.setdefault(g["away_id"], []).append(away_score)
            runs_against.setdefault(g["away_id"], []).append(home_score)
    return games


# --------------------------------------------------------------------------
# FIP del pitcher probable, reconstruido desde su gameLog (sin lookahead)
# --------------------------------------------------------------------------

def fetch_pitcher_gamelog(pitcher_id, season, cache_dir, sleep_seconds=DEFAULT_SLEEP_SECONDS):
    """Cada start de un pitcher en la temporada, ordenado por fecha, con IP
    (en entradas reales, ya convertidas) y HR/BB/HBP/K de ESE partido
    puntual -- la base para acumular "FIP antes de la fecha D" partido a
    partido (ver AUDITORÍA del header y FIP_CONSTANT en mlb_signal_engine.py)."""
    cache_key = f"pitcher_{pitcher_id}_{season}_v{CACHE_SCHEMA_VERSION}"
    cached = _cache_load(cache_dir, cache_key)
    if cached is not None:
        return cached

    data = _get(f"/people/{pitcher_id}/stats", {"stats": "gameLog", "group": "pitching", "season": season})
    time.sleep(sleep_seconds)
    starts = []
    if data:
        for entry in data.get("stats", []):
            for split in entry.get("splits", []):
                stat = split.get("stat", {})
                game_date = split.get("date")
                if not game_date:
                    continue
                starts.append({
                    "date": game_date,
                    "ip": _parse_innings_pitched(stat.get("inningsPitched")),
                    "hr": float(stat.get("homeRuns", 0) or 0),
                    "bb": float(stat.get("baseOnBalls", 0) or 0),
                    "hbp": float(stat.get("hitByPitch", 0) or 0),
                    "so": float(stat.get("strikeOuts", 0) or 0),
                })
    starts.sort(key=lambda s: s["date"])
    _cache_save(cache_dir, cache_key, starts)
    return starts


def fip_asof(gamelog, game_date, min_innings=MIN_INNINGS_FOR_ERA, fip_constant=FIP_CONSTANT):
    """FIP acumulado del pitcher estrictamente ANTES de game_date (no
    incluye el propio start que se está evaluando), o None si no llegó a
    min_innings todavía -- mismo umbral y mismo motivo que antes con ERA
    (una salida atípica temprana no debería dominar el ajuste)."""
    cum_ip = cum_hr = cum_bb = cum_hbp = cum_so = 0.0
    for start in gamelog:
        if start["date"] >= game_date:
            break
        cum_ip += start["ip"]
        cum_hr += start["hr"]
        cum_bb += start["bb"]
        cum_hbp += start["hbp"]
        cum_so += start["so"]
    if cum_ip < min_innings:
        return None
    return round((13 * cum_hr + 3 * (cum_bb + cum_hbp) - 2 * cum_so) / cum_ip + fip_constant, 2)


def attach_pitcher_fip(games, season, cache_dir, sleep_seconds=DEFAULT_SLEEP_SECONDS):
    """Agrega fip_home_asof/fip_away_asof a cada partido. Descarga el
    gameLog de cada pitcher único UNA vez (no una vez por partido en el que
    aparece) -- un titular hace ~30 starts/temporada, así que esto evita
    30x llamadas redundantes."""
    pitcher_ids = set()
    for g in games:
        if g["home_pitcher_id"]:
            pitcher_ids.add(g["home_pitcher_id"])
        if g["away_pitcher_id"]:
            pitcher_ids.add(g["away_pitcher_id"])

    log.info(f"{season}: descargando gameLog de {len(pitcher_ids)} pitchers únicos...")
    gamelogs = {}
    for i, pid in enumerate(sorted(pitcher_ids)):
        gamelogs[pid] = fetch_pitcher_gamelog(pid, season, cache_dir, sleep_seconds)
        if (i + 1) % 25 == 0:
            log.info(f"{season}: {i + 1}/{len(pitcher_ids)} gameLogs descargados.")

    for g in games:
        g["fip_home_asof"] = (
            fip_asof(gamelogs[g["home_pitcher_id"]], g["date"]) if g["home_pitcher_id"] in gamelogs else None
        )
        g["fip_away_asof"] = (
            fip_asof(gamelogs[g["away_pitcher_id"]], g["date"]) if g["away_pitcher_id"] in gamelogs else None
        )
    return games


# --------------------------------------------------------------------------
# Modelo: reutiliza blended_win_pct/combine_components de mlb_signal_engine.py
# tal cual, para no duplicar la matemática -- ver AUDITORÍA del header.
# --------------------------------------------------------------------------

def compute_prob_home(game, home_field_edge=HOME_FIELD_EDGE, pitcher_era_scale=PITCHER_ERA_SCALE,
                       pitcher_edge_cap=PITCHER_EDGE_CAP, season_weight=SEASON_FORM_WEIGHT,
                       min_games=MIN_GAMES_FOR_FORM):
    """Llama a combine_components() (mlb_signal_engine.py) con los
    componentes ya reconstruidos sin lookahead, en vez de pegarle a la API
    en vivo -- ver AUDITORÍA del header sobre por qué ya no se duplica esta
    matemática acá. Devuelve (raw_prob antes del cap 0.05-0.95, prob ya con
    ese cap aplicado) -- mismos dos valores que generate_mlb_signal() guarda
    como raw_my_prob/my_prob."""
    home_form = None if game["home_win_pct_season"] is None else {
        "win_pct": game["home_win_pct_season"], "last_ten_pct": game["home_last10_pct"],
        "games_played": game.get("home_games_played"), "pyth_win_pct": game.get("home_pyth_win_pct"),
    }
    away_form = None if game["away_win_pct_season"] is None else {
        "win_pct": game["away_win_pct_season"], "last_ten_pct": game["away_last10_pct"],
        "games_played": game.get("away_games_played"), "pyth_win_pct": game.get("away_pyth_win_pct"),
    }
    home_pct, _ = blended_win_pct(home_form, season_weight, min_games)
    away_pct, _ = blended_win_pct(away_form, season_weight, min_games)

    raw_prob, prob, _edge = combine_components(
        home_pct, away_pct, game["fip_home_asof"], game["fip_away_asof"],
        home_field_edge_logodds=home_field_edge,
        pitcher_era_scale=pitcher_era_scale, pitcher_edge_cap=pitcher_edge_cap,
    )
    return raw_prob, prob


# --------------------------------------------------------------------------
# Métricas
# --------------------------------------------------------------------------

def brier_score(games, prob_key):
    scored = [g for g in games if g.get(prob_key) is not None]
    if not scored:
        return None
    return sum((g[prob_key] - (1.0 if g["home_won"] else 0.0)) ** 2 for g in scored) / len(scored)


def calibration_table(games, prob_key, n_buckets=10):
    """Predicho vs. real por decil de probabilidad -- mismo formato que las
    auditorías de calibración ya hechas a mano sobre las señales en vivo
    (ver AUDITORÍA 09/09 y 12/09 en config.py/mlb_signal_engine.py), acá
    sobre miles de partidos en vez de 20-40."""
    rows = []
    for lo in range(0, 100, 100 // n_buckets):
        hi = lo + 100 // n_buckets
        bucket = [g for g in games if g.get(prob_key) is not None and lo / 100 <= g[prob_key] < hi / 100]
        if hi == 100:  # incluir 1.0 exacto en el último bucket
            bucket += [g for g in games if g.get(prob_key) == 1.0]
        if not bucket:
            continue
        avg_pred = sum(g[prob_key] for g in bucket) / len(bucket)
        actual = sum(1 for g in bucket if g["home_won"]) / len(bucket)
        rows.append({"range": f"{lo}-{hi}%", "n": len(bucket), "pred": avg_pred, "actual": actual})
    return rows


def log_calibration(label, games, prob_key):
    log.info(f"--- Calibración: {label} ---")
    for row in calibration_table(games, prob_key):
        log.info(f"  {row['range']:>8}  n={row['n']:<5}  predicho={row['pred']*100:5.1f}%  real={row['actual']*100:5.1f}%")
    bs = brier_score(games, prob_key)
    if bs is not None:
        log.info(f"  Brier score: {bs:.4f}  (0.25 = no saber nada, 0 = perfecto)")


# --------------------------------------------------------------------------
# Sweep de constantes -- responde a "fitear/validar los pesos contra un
# dataset con órdenes de magnitud más de datos"
# --------------------------------------------------------------------------

def sweep(games):
    """Grid search chico sobre las 3 constantes con más impacto directo en
    my_prob (deja MIN_INNINGS_FOR_ERA y MIN_GAMES_FOR_FORM fijos -- afectan
    qué partidos tienen datos no-None, no algo que tenga sentido barrer
    junto con el resto en la misma pasada, mismo criterio que antes con
    MIN_INNINGS_FOR_ERA). Barato: no pega a la API, solo recalcula
    combine_components() sobre el dataset ya en memoria.

    home_field_options en espacio de LOG-ODDS (ver AUDITORÍA 13/09/2026 en
    HOME_FIELD_EDGE, mlb_signal_engine.py) -- 0.16 (el default actual)
    equivale a ~54% en un partido 50-50; el rango barre desde sin ventaja de
    local (0.0) hasta el doble del default (0.32, ~58%)."""
    home_field_options = [0.0, 0.08, 0.16, 0.24, 0.32]
    pitcher_scale_options = [0.0, 0.025, 0.05, 0.075, 0.10]
    season_weight_options = [0.5, 0.6, 0.7, 0.8, 0.9]

    results = []
    for hfe in home_field_options:
        for scale in pitcher_scale_options:
            for sw in season_weight_options:
                for g in games:
                    _, g["_sweep_prob"] = compute_prob_home(
                        g, home_field_edge=hfe, pitcher_era_scale=scale, season_weight=sw,
                    )
                bs = brier_score(games, "_sweep_prob")
                results.append({
                    "home_field_edge": hfe, "pitcher_era_scale": scale, "season_form_weight": sw, "brier": bs,
                })

    results.sort(key=lambda r: r["brier"])
    log.info("=== TOP 10 combinaciones por Brier score (grid search) ===")
    for r in results[:10]:
        log.info(
            f"  HOME_FIELD_EDGE={r['home_field_edge']:.3f}  PITCHER_ERA_SCALE={r['pitcher_era_scale']:.3f}  "
            f"SEASON_FORM_WEIGHT={r['season_form_weight']:.2f}  ->  Brier={r['brier']:.4f}"
        )
    log.info(
        f"(valores actuales: HOME_FIELD_EDGE={HOME_FIELD_EDGE}, PITCHER_ERA_SCALE={PITCHER_ERA_SCALE}, "
        f"SEASON_FORM_WEIGHT={SEASON_FORM_WEIGHT} -- comparar contra el Brier de arriba en la corrida sin --sweep)"
    )
    for g in games:
        g.pop("_sweep_prob", None)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seasons", default="2023,2024,2025", help="temporadas separadas por coma, ej. 2023,2024,2025")
    parser.add_argument("--cache-dir", default=DEFAULT_CACHE_DIR, help="dónde cachear schedule/gameLogs descargados")
    parser.add_argument("--output", help="CSV con el detalle de cada partido (componentes crudos + prob + resultado) "
                                          "-- insumo para fitear una curva de calibración isotonic/Platt después")
    parser.add_argument("--sweep", action="store_true", help="grid search de HOME_FIELD_EDGE/PITCHER_ERA_SCALE/SEASON_FORM_WEIGHT")
    parser.add_argument("--oos-frac", type=float, default=0.0,
                         help="fracción final del período (0-1) a reportar aparte como out-of-sample, "
                              "ej. 0.3 = último 30%% cronológico. 0 (default) = sin split.")
    args = parser.parse_args()

    seasons = [int(s.strip()) for s in args.seasons.split(",") if s.strip()]
    all_games = []
    for season in seasons:
        games = fetch_season_games(season, args.cache_dir)
        if not games:
            log.warning(f"{season}: sin partidos -- se salta.")
            continue
        build_team_form_asof(games)
        attach_pitcher_fip(games, season, args.cache_dir)
        all_games.extend(games)

    if not all_games:
        log.error("No se descargó ningún partido -- revisar conectividad con statsapi.mlb.com.")
        return

    for g in all_games:
        g["raw_prob_home"], g["prob_home"] = compute_prob_home(g)
        g["prob_home_clipped"] = min(max(g["prob_home"], Config.MLB_PROB_CLIP_MIN), Config.MLB_PROB_CLIP_MAX)
        g["has_pitcher_data"] = g["fip_home_asof"] is not None and g["fip_away_asof"] is not None

    with_pitcher = [g for g in all_games if g["has_pitcher_data"]]
    log.info(f"=== TOTAL: {len(all_games)} partidos ({len(with_pitcher)} con FIP de ambos probables ya con muestra suficiente) ===")

    log_calibration("prob_home (cap 0.05-0.95, sin el clip de Config)", all_games, "prob_home")
    log_calibration("prob_home_clipped (con el clip 40-60% actual de producción)", all_games, "prob_home_clipped")
    if with_pitcher:
        log_calibration("prob_home -- solo partidos con FIP de ambos probables", with_pitcher, "prob_home")

    if args.oos_frac and 0 < args.oos_frac < 1:
        all_games.sort(key=lambda g: g["date"] or "")
        cutoff_idx = int(len(all_games) * (1 - args.oos_frac))
        in_sample, out_sample = all_games[:cutoff_idx], all_games[cutoff_idx:]
        log.info(f"=== Split walk-forward: in-sample hasta {out_sample[0]['date'] if out_sample else 'N/A'} ===")
        log_calibration("in-sample", in_sample, "prob_home")
        log_calibration("out-of-sample", out_sample, "prob_home")

    if args.sweep:
        sweep(all_games)

    if args.output:
        fieldnames = [
            "date", "game_pk", "home_id", "away_id",
            "home_win_pct_season", "home_last10_pct", "home_games_played", "home_pyth_win_pct",
            "away_win_pct_season", "away_last10_pct", "away_games_played", "away_pyth_win_pct",
            "fip_home_asof", "fip_away_asof",
            "raw_prob_home", "prob_home", "prob_home_clipped", "home_won",
        ]
        with open(args.output, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(all_games)
        log.info(f"Detalle de {len(all_games)} partidos guardado en {args.output}")


if __name__ == "__main__":
    main()
