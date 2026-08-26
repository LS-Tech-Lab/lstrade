"""
Reinicia manualmente el circuit breaker cuando corrés en modo serverless
(Supabase). El equivalente de `python main.py --reset-halt` del modo VPS,
pero como endpoint porque en serverless no hay una consola donde correr eso.

Protegido con el mismo CRON_SECRET que usás para /api/cycle — no es
información pública, y esta acción para reiniciar el sistema no debería
ser gatillable por cualquiera.
"""
from http.server import BaseHTTPRequestHandler
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from supabase_db import SupabaseDatabase


class handler(BaseHTTPRequestHandler):
    def _respond(self, code, payload):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def do_POST(self):
        expected = os.environ.get("CRON_SECRET")
        auth = self.headers.get("Authorization", "")
        if expected and auth != f"Bearer {expected}":
            self._respond(401, {"error": "unauthorized"})
            return
        try:
            db = SupabaseDatabase(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
            db.set_state("trading_halted", "0")
            db.set_state("halt_reason", "")
            db.set_state("halt_notified", "0")
            self._respond(200, {"status": "reset"})
        except Exception as e:
            self._respond(500, {"status": "error", "detail": str(e)})
