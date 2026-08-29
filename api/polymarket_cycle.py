"""
⚠️ ESTE ARCHIVO NO SE DESPLIEGA EN VERCEL — ver app.py.

La ruta real en producción es /api/polymarket_cycle definido en app.py
(función polymarket_cycle_get/polymarket_cycle_post ahí). Ver el aviso en
api/cycle.py para el porqué. Este archivo queda como referencia legible
de la misma lógica en aislamiento — pegar solo esto en GitHub NO alcanza,
los cambios van en app.py.

Función serverless — un ciclo de escaneo de Polymarket, pensada para ser
disparada por un cron externo (ver .github/workflows/trigger-cycle.yml)
cada ~10 min, igual que api/cycle.py hace con el bot de cripto.

Separado de api/cycle.py a propósito: son dos mercados con lógica de riesgo
independiente, y mezclarlos en una sola función haría que un error de uno
tumbe al otro y que compitan por el mismo presupuesto de 10s del plan
Hobby de Vercel.

El estado de dedup de avisos (antes un JSON en disco, polymarket_state.json)
vive en la tabla polymarket_notify_state de Supabase — un archivo local no
sirve acá porque cada invocación arranca con filesystem limpio.
"""
from http.server import BaseHTTPRequestHandler
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
from supabase_db import SupabaseDatabase
from polymarket_client import PolymarketClient
from polymarket_main import SupabaseNotifyStateAdapter, run_polymarket_cycle_serverless
from telegram_notifier import TelegramNotifier


def run_cycle():
    config = Config
    db = SupabaseDatabase(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    client = PolymarketClient(config)
    notifier = TelegramNotifier(config)
    state_store = SupabaseNotifyStateAdapter(
        db, resend_cooldown_hours=getattr(config, "POLYMARKET_RESEND_COOLDOWN_HOURS", 6.0)
    )

    # Configurables sin tocar código — bajalos si la función sigue dando
    # timeout en tu plan, subilos si tenés más presupuesto (Pro/Fluid).
    top_n = int(os.environ.get("POLYMARKET_SERVERLESS_TOP_N", "15"))
    time_budget = float(os.environ.get("POLYMARKET_TIME_BUDGET_SECONDS", "7.5"))
    request_timeout = float(os.environ.get("POLYMARKET_REQUEST_TIMEOUT_SECONDS", "5"))

    return run_polymarket_cycle_serverless(
        config, client, notifier, db, state_store,
        top_n=top_n, time_budget_seconds=time_budget, request_timeout=request_timeout,
    )


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
            result = run_cycle()
            self._respond(200, result)
        except Exception as e:
            self._respond(500, {"status": "error", "detail": str(e)})
