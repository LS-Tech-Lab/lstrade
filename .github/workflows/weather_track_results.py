"""
⚠️ ESTE ARCHIVO NO SE DESPLIEGA EN VERCEL — ver app.py.

La ruta real en producción es /api/weather_track_results definida en
app.py (función run_weather_track_results / weather_track_results_get /
weather_track_results_post ahí). Ver el aviso en api/polymarket_resolve.py
para el porqué de este patrón. Este archivo queda como referencia legible
de la misma lógica en aislamiento — pegar solo esto en GitHub NO alcanza,
los cambios van en app.py.

Resuelve las señales de clima que run_weather_cycle() registró en
weather_signals (ver schema.sql) y que todavía no tienen outcome. No hay
plan de entrada/target/stop como en Polymarket genérico — acá lo que
importa es si el bucket de temperatura terminó ganando (YES) o no (NO).
Se aproxima mirando si el precio del token convergió cerca de 1.0 o 0.0
(mismo endpoint de price history que ya usa polymarket_client.py), sin
necesidad de un endpoint de resolución oficial aparte.

Pensado para correr con mucha menos frecuencia que weather_cycle — los
mercados de clima se resuelven en horas/días, no en minutos.
"""
from http.server import BaseHTTPRequestHandler
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
from supabase_db import SupabaseDatabase
from polymarket_client import PolymarketClient

WEATHER_RESOLVED_YES_THRESHOLD = 0.98
WEATHER_RESOLVED_NO_THRESHOLD = 0.02


def run_weather_track_results():
    config = Config
    db = SupabaseDatabase(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    client = PolymarketClient(config)

    open_signals = db.get_open_weather_signals()
    if not open_signals:
        return {"status": "no_open_signals"}

    resolved = []
    for sig in open_signals:
        token_id = sig.get("yes_token_id")
        if not token_id:
            continue
        history = client.fetch_price_history(token_id, interval="1h", fidelity=60)
        if not history:
            continue
        current_price = history[-1]["p"]

        if current_price >= WEATHER_RESOLVED_YES_THRESHOLD:
            outcome = "yes"
        elif current_price <= WEATHER_RESOLVED_NO_THRESHOLD:
            outcome = "no"
        else:
            continue

        db.resolve_weather_signal(sig["id"], outcome)
        resolved.append({"condition_id": sig["condition_id"], "outcome": outcome})

    return {"status": "ok", "resolved": resolved, "still_open": len(open_signals) - len(resolved)}


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
            result = run_weather_track_results()
            self._respond(200, result)
        except Exception as e:
            self._respond(500, {"status": "error", "detail": str(e)})
