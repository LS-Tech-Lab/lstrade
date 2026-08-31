"""
Versión serverless del análisis de clima (weather_signal_engine.py).

Corre SEPARADO del ciclo de precio/momentum (api/polymarket_cycle.py):
un evento de clima necesita NWS points + forecast + METAR + TAF (varias
llamadas de red secuenciales por evento), que no entran cómodas en el
mismo presupuesto de 10s del ciclo principal sin arriesgar que ese ciclo
se quede sin tiempo para lo que ya venía haciendo. El clima tampoco
necesita la granularidad de 10-15 min del precio — el pronóstico no
cambia tan rápido — así que el cron externo puede disparar esto con
menos frecuencia (ver .github/workflows/trigger-weather-cycle.yml).

Mismo patrón que polymarket_cycle.py: BaseHTTPRequestHandler + CRON_SECRET
en el header Authorization.
"""
from http.server import BaseHTTPRequestHandler
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
from supabase_db import SupabaseDatabase
from polymarket_client import PolymarketClient
from telegram_notifier import TelegramNotifier
from weather_signal_engine import (
    generate_weather_signal,
    build_weather_memo,
    resolve_station,
    WeatherNotifyStateStore,
)


def run_weather_cycle():
    config = Config
    if not config.WEATHER_ANALYSIS_ENABLED:
        return {"status": "disabled"}

    db = SupabaseDatabase(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    client = PolymarketClient(config)
    notifier = TelegramNotifier(config)
    state_store = WeatherNotifyStateStore(
        db, resend_cooldown_hours=config.WEATHER_RESEND_COOLDOWN_HOURS
    )

    started = time.monotonic()
    time_budget = float(os.environ.get("WEATHER_TIME_BUDGET_SECONDS", "8.0"))

    def time_left():
        return time_budget - (time.monotonic() - started)

    events = client.fetch_weather_events(limit=20)
    if not events:
        return {"status": "no_events"}

    # Ver el comentario largo en app.py (misma lógica) — se prioriza lo que
    # el motor puede resolver de verdad (resolve_station) antes de ordenar
    # por liquidez, para no llenar el top_n con ciudades sin estación
    # cubierta.
    top_n = int(os.environ.get("WEATHER_TOP_N", "3"))
    events = sorted(
        events,
        key=lambda e: (
            0 if resolve_station(e.get("title") or "", override_icao=getattr(config, "WEATHER_STATION_OVERRIDE", None)) else 1,
            -sum(m.get("liquidity", 0) for m in e["markets"]),
        ),
    )[:top_n]

    sent = 0
    scanned = 0
    detail = []
    for event in events:
        if time_left() < 1.0:
            detail.append({"title": event["title"], "status": "sin_tiempo"})
            break
        scanned += 1
        try:
            signal = generate_weather_signal(event, config, min_ev=config.WEATHER_MIN_EV)
        except Exception as e:
            detail.append({"title": event["title"], "status": "error", "error": str(e)})
            continue

        detail.append({"title": event["title"], "status": signal.get("status")})
        if signal.get("status") != "ok" or not signal.get("best_trade"):
            continue

        best = signal["best_trade"]
        if not state_store.should_notify(best["condition_id"], best["ev"]):
            continue

        memo = build_weather_memo(signal, markdown=True)
        if not memo:
            continue
        try:
            notifier.send_message(memo)
            state_store.record_notified(best["condition_id"], best["ev"])
            sent += 1
            # NUEVO: registra la probabilidad estimada para poder medir
            # después (weather_track_results) si el modelo está calibrado —
            # ver weather_signals en schema.sql.
            try:
                db.record_weather_signal(
                    best["condition_id"], best["question"], event["title"],
                    signal["station"].get("icao"), best["my_prob"], best["market_price"],
                    best["ev"], signal["center_estimate_f"], signal["sigma"],
                    best.get("yes_token_id"),
                )
            except Exception as e:
                detail.append({"title": event["title"], "status": "error_registro", "error": str(e)})
        except Exception as e:
            detail.append({"title": event["title"], "status": "error_envio", "error": str(e)})

    return {
        "status": "ok",
        "events_found": len(events),
        "events_scanned": scanned,
        "signals_sent": sent,
        "detail": detail,
    }


class handler(BaseHTTPRequestHandler):
    def _respond(self, code, payload):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def do_POST(self):
        self.do_GET()

    def do_GET(self):
        expected = os.environ.get("CRON_SECRET")
        auth = self.headers.get("Authorization", "")
        if expected and auth != f"Bearer {expected}":
            self._respond(401, {"error": "unauthorized"})
            return
        try:
            result = run_weather_cycle()
            self._respond(200, result)
        except Exception as e:
            self._respond(500, {"status": "error", "detail": str(e)})
