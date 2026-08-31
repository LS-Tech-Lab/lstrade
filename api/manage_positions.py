"""
Función serverless separada de api/cycle.py a propósito (ver el comentario
en schema.sql): revisa las posiciones de cripto abiertas en modo papel y,
si el precio actual ya tocó el target o el stop, las cierra y calcula el
resultado en R — es la pieza que faltaba para que `stats_summary()` (win
rate, expectancy) tuviera datos reales en el modo serverless.

No incluye trailing stop dinámico (eso vive en position_manager.py, el
equivalente del modo VPS/local) — acá se prioriza que la función corra
rápido y sin sorpresas dentro del timeout de 10s del plan Hobby de Vercel.
Se puede sumar como una iteración aparte si hace falta.

Pensada para correr cada pocos minutos vía GitHub Actions, igual que
api/cycle.py (ver .github/workflows/trigger-manage-positions.yml).
"""
from http.server import BaseHTTPRequestHandler
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
from supabase_db import SupabaseDatabase
from exchange_client import ExchangeClient
from telegram_notifier import TelegramNotifier


def run_manage_positions():
    config = Config
    db = SupabaseDatabase(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    exchange_client = ExchangeClient(config)
    notifier = TelegramNotifier(config)

    open_trades = db.get_open_trades()
    if not open_trades:
        return {"status": "no_open_trades"}

    closed = []
    errors = []
    for trade in open_trades:
        symbol = trade["symbol"]
        try:
            ticker = exchange_client.fetch_ticker(symbol)
            current_price = ticker["last"]
        except Exception as e:
            errors.append(f"{symbol}: {e}")
            continue

        direction = trade["direction"]
        target_price = trade["target_price"]
        current_stop = trade["current_stop"]

        hit_target = (current_price >= target_price) if direction == "LONG" else (current_price <= target_price)
        hit_stop = (current_price <= current_stop) if direction == "LONG" else (current_price >= current_stop)

        if not (hit_target or hit_stop):
            continue

        outcome = "target" if hit_target else "stop"
        exit_price = target_price if hit_target else current_stop
        r_multiple = db.close_trade_with_outcome(trade, exit_price, outcome)
        if r_multiple is False:
            continue  # otra invocación ya cerró este trade primero

        emoji = "✅" if outcome == "target" else "🛑"
        r_text = f" ({r_multiple:+.2f}R)" if r_multiple is not None else ""
        notifier.send_message(
            f"{emoji} *Posición cerrada* — {symbol} {direction}\n"
            f"Resultado: {outcome.upper()}{r_text}\nSalida: `{exit_price:.6f}`"
        )
        closed.append({"symbol": symbol, "outcome": outcome, "r_multiple": r_multiple})

    return {"status": "ok", "closed": closed, "still_open": len(open_trades) - len(closed), "errors": errors}


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
            result = run_manage_positions()
            self._respond(200, result)
        except Exception as e:
            self._respond(500, {"status": "error", "detail": str(e)})
