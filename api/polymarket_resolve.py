"""
Función serverless — revisa señales de Polymarket ya enviadas (con plan de
entrada/target/stop) y resuelve las que ya tocaron alguno de los dos. Esto
es lo que permite calcular win rate real del módulo (antes solo existía en
polymarket_track_results.py corriendo contra SQLite en el loop local).

Reutiliza check_open_signals() de polymarket_track_results.py sin tocarlo:
esa función ya trabaja contra cualquier objeto `db` que tenga
get_open_polymarket_signals() / resolve_polymarket_signal(), que es
exactamente lo que SupabaseDatabase ya implementa.

Pensada para correr con menos frecuencia que el ciclo de escaneo — las
señales de Polymarket no se resuelven en minutos, cada 30 min alcanza de
sobra (ver .github/workflows/trigger-cycle.yml).
"""
from http.server import BaseHTTPRequestHandler
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
from supabase_db import SupabaseDatabase
from polymarket_client import PolymarketClient
from polymarket_track_results import check_open_signals
from telegram_notifier import TelegramNotifier


def run_resolve():
    config = Config
    db = SupabaseDatabase(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    client = PolymarketClient(config)
    notifier = TelegramNotifier(config)

    open_before = db.get_open_polymarket_signals()
    check_open_signals(db, client, notifier)
    open_after = db.get_open_polymarket_signals()

    return {
        "status": "ok",
        "open_before": len(open_before),
        "resolved": len(open_before) - len(open_after),
        "open_after": len(open_after),
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
            result = run_resolve()
            self._respond(200, result)
        except Exception as e:
            self._respond(500, {"status": "error", "detail": str(e)})
