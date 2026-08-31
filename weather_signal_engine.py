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
    return {"User-Agent": ua, "Accept": "application/json"}


def fetch_nws_guidance(station, config, timeout=DEFAULT_TIMEOUT):
    """Guía de pronóstico oficial de NWS para el punto de la estación.
    api.weather.gov exige un User-Agent identificable (no un navegador
    genérico) — configurar WEATHER_USER_AGENT con un contacto real o NWS
    puede empezar a bloquear las requests."""
    if station.get("lat") is None:
        return None
    try:
        headers = _headers(config)
        points = requests.get(
            f"{NWS_API}/points/{station['lat']},{station['lon']}",
            headers=headers, timeout=timeout,
        ).json()
        forecast_url = points["properties"]["forecast"]
        forecast = requests.get(forecast_url, headers=headers, timeout=timeout).json()
        periods = forecast["properties"]["periods"]
        today_day = next((p for p in periods if p.get("isDaytime")), periods[0])
        return {
            "forecast_high_f": today_day.get("temperature"),
            "short_forecast": today_day.get("shortForecast"),
            "issued": forecast["properties"].get("updated"),
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


def fetch_metar(icao, config, hours=3, timeout=DEFAULT_TIMEOUT):
    """Observación en vivo + máxima de 6h de los remarks, tal como pide
    STEP 1 de la skill (watch the 6-hour max temperature group)."""
    try:
        headers = _headers(config)
        resp = requests.get(
            f"{AWC_API}/metar",
            params={"ids": icao, "format": "json", "hours": hours},
            headers=headers, timeout=timeout,
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


def fetch_taf(icao, config, timeout=DEFAULT_TIMEOUT):
    """TAF para timing de nubes/tormenta — lo que realmente topea la
    máxima del día (STEP 1/2 de la skill)."""
    try:
        headers = _headers(config)
        resp = requests.get(
            f"{AWC_API}/taf",
            params={"ids": icao, "format": "json"},
            headers=headers, timeout=timeout,
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

    if metar and metar.get("temp_f") is not None and 9 <= hour <= 14:
        current = metar["temp_f"]
        six_hr_max = metar.get("six_hr_max_f")
        reference = max(current, six_hr_max) if six_hr_max else current
        expected_at_this_hour = base - 6  # normalmente faltan ~5-8°F para la máxima a media mañana
        delta = reference - expected_at_this_hour
        if abs(delta) >= 2:
            adjustment += max(-3.0, min(3.0, delta * 0.4))
            notes.append(f"Trayectoria matutina {delta:+.1f}°F vs. lo esperado a esta hora — ajuste {adjustment:+.1f}°F.")

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


def generate_weather_signal(event, config, min_ev=0.15):
    """
    Genera una señal de clima para un evento de Polymarket agrupado por
    buckets (mercados YES/NO por rango de temperatura del mismo día/estación).

    `event`: {"title": str, "markets": [market_parseado, ...]} — cada market
    viene de PolymarketClient.parse_market_for_analysis (o compatible).

    Devuelve un dict con "status" en {"no_station", "no_buckets", "no_data",
    "ok"} — nunca lanza excepción por datos faltantes, siempre degrada.
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

    nws = fetch_nws_guidance(station, config)
    metar = fetch_metar(station["icao"], config)
    taf = fetch_taf(station["icao"], config)

    center, notes, penalty = estimate_adjusted_high(nws, metar, taf, station)
    if center is None:
        return {"status": "no_data", "title": title, "station": station}

    base_sigma = getattr(config, "WEATHER_BASE_SIGMA_F", 1.6)
    distribution, sigma = build_bucket_distribution(center, buckets, base_sigma=base_sigma, confidence_penalty=penalty)

    rows = []
    best = None
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
            # Link directo al bucket específico si el mercado individual
            # trae su propio slug; si no, más abajo se usa el link del
            # evento completo como mejor esfuerzo.
            "url": m.get("url") or event.get("url"),
        }
        rows.append(row)
        if ev is not None and ev >= min_ev and (best is None or ev > best["ev"]):
            best = row

    rows.sort(key=lambda r: (r["ev"] if r["ev"] is not None else -999), reverse=True)

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
        "settlement_verified": station.get("verified", False),
        "disclaimer": DISCLAIMER,
    }


# ---------------------------------------------------------------------------
# STEP 4 — Formato de reporte (versión compacta para Telegram)
# ---------------------------------------------------------------------------
def build_weather_memo(signal, markdown=True):
    if not signal or signal.get("status") != "ok":
        return None
    st = signal["station"]
    lines = []
    title_txt = signal["title"][:70]
    lines.append(f"🌡️ *ANÁLISIS DE CLIMA* — {title_txt}" if markdown else f"🌡️ ANÁLISIS DE CLIMA — {title_txt}")
    lines.append("")
    verified_flag = "✅ verificada" if signal["settlement_verified"] else "⚠️ SIN VERIFICAR — revisar manualmente"
    lines.append(f"⚖️ Estación: {st.get('name', st.get('icao'))} ({verified_flag})")
    if st.get("note"):
        lines.append(f"   ℹ️ {st['note']}")
    lines.append(f"📈 Estimación de máxima: {signal['center_estimate_f']}°F (±{signal['sigma']:.1f}°F, penalización de confianza {signal['confidence_penalty']:.2f})")
    lines.append("")
    lines.append("📊 Buckets (mi prob. vs mercado):")
    for row in signal["buckets"][:6]:
        ev_txt = f"{row['ev']*100:+.1f}%" if row["ev"] is not None else "N/A"
        q = row["question"][:45]
        lines.append(f"  • {q}: yo {row['my_prob']*100:.1f}% | mkt ${row['market_price']:.3f} | EV {ev_txt}")
    lines.append("")
    lines.append("🌦️ Física del día:")
    for note in signal["physics_notes"][:4]:
        lines.append(f"  • {note}")
    lines.append("")
    if signal["best_trade"]:
        bt = signal["best_trade"]
        lines.append(f"🎯 Mejor EV: {bt['question'][:60]}")
        lines.append(f"   EV {bt['ev']*100:+.1f}% @ ${bt['market_price']:.3f} | mi prob {bt['my_prob']*100:.1f}%")
        if bt.get("url"):
            lines.append(f"🔗 {bt['url']}")
    else:
        lines.append("🎯 Sin edge suficiente hoy — pasar es la disciplina correcta.")
        if signal.get("url"):
            lines.append(f"🔗 {signal['url']}")
    lines.append("")
    disclaimer = signal["disclaimer"]
    lines.append(f"_{disclaimer}_" if markdown else disclaimer)
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
