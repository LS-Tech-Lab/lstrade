"""
Versión serverless de polymarket_track_results.py (VPS/local). Revisa las
señales de Polymarket registradas por api/polymarket_cycle.py que todavía
no tienen resultado, trae el precio más reciente de ese token, y si ya
tocó target o stop lo marca en Supabase — sin esto, polymarket_stats_summary()
se queda siempre en cero porque ninguna señal se resuelve nunca.
"""
from http.server import BaseHTTPRequestHandler
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
from supabase_db import SupabaseDatabase
from polymarket_client import PolymarketClient
from telegram_notifier import TelegramNotifier


def run_polymarket_track_results():
    config = Config
    db = SupabaseDatabase(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    client = PolymarketClient(config)
    notifier = TelegramNotifier(config)

    open_signals = db.get_open_polymarket_signals()
    if not open_signals:
        return {"status": "no_open_signals"}

    resolved = []
    for sig in open_signals:
        history = client.fetch_price_history(sig["token_id"], interval="1h", fidelity=60)
        if not history:
            continue
        current_price = history[-1]["p"]

        hit_target = current_price >= sig["target"]
        hit_stop = current_price <= sig["stop"]
        if not (hit_target or hit_stop):
            continue

        outcome = "target" if hit_target else "stop"
        exit_price = sig["target"] if hit_target else sig["stop"]
        db.resolve_polymarket_signal(sig["id"], exit_price, outcome)

        emoji = "✅" if outcome == "target" else "🛑"
        notifier.send_message(
            f"{emoji} *Señal Polymarket resuelta* — {sig['question'][:70]}\n"
            f"Dirección: {sig['direction']} | Resultado: {outcome.upper()}\n"
            f"Entrada: `{sig['entry']:.3f}` → Salida: `{exit_price:.3f}`"
        )
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
            result = run_polymarket_track_results()
            self._respond(200, result)
        except Exception as e:
            self._respond(500, {"status": "error", "detail": str(e)})
