"""
Webhook de Telegram. Configurá tu bot para que mande los updates acá
(ver README, sección Vercel + Supabase) y este endpoint resuelve la
decisión pendiente que dejó api/cycle.py — incluyendo la ejecución
REAL de la orden si tocaste "Aprobar".
"""
from http.server import BaseHTTPRequestHandler
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
from supabase_db import SupabaseDatabase
from exchange_client import ExchangeClient
from executor import Executor
from telegram_notifier import TelegramNotifier

DECISION_MAP = {"approve": "approved", "watchlist": "watchlist", "reject": "rejected"}


def handle_update(update):
    config = Config
    db = SupabaseDatabase(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    notifier = TelegramNotifier(config)

    cq = update.get("callback_query")
    if not cq:
        return {"status": "ignored"}

    data = cq.get("data")
    message_id = cq.get("message", {}).get("message_id")
    decision = DECISION_MAP.get(data)

    if not decision or message_id is None:
        notifier.answer_callback(cq["id"], "Acción no reconocida")
        return {"status": "ignored"}

    pending = db.claim_pending_decision(message_id)
    if not pending:
        notifier.answer_callback(cq["id"], "Esta decisión ya fue resuelta o expiró")
        return {"status": "not_found"}

    symbol = pending["symbol"]
    signal = pending["signal"]
    risk_report = pending["risk_report"]
    plan = pending["plan"]

    order_detail = None
    if decision == "approved":
        exchange_client = ExchangeClient(config)
        executor = Executor(exchange_client, config)
        order_detail = executor.execute(symbol, plan)
        notifier.answer_callback(cq["id"], "Orden ejecutada")
        notifier.send_message(f"\u2705 Orden ejecutada en {symbol}: {order_detail.get('status')}")
    elif decision == "watchlist":
        notifier.answer_callback(cq["id"], "Agregado a watchlist")
    else:
        notifier.answer_callback(cq["id"], "Rechazado")

    db.log_decision(symbol, signal, risk_report, plan, decision, order_detail)
    return {"status": "resolved", "decision": decision, "symbol": symbol}


class handler(BaseHTTPRequestHandler):
    def _respond(self, code, payload):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def do_POST(self):
        secret_header = self.headers.get("X-Telegram-Bot-Api-Secret-Token")
        expected = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
        if expected and secret_header != expected:
            self._respond(401, {"error": "unauthorized"})
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b"{}"
        try:
            update = json.loads(body or b"{}")
            result = handle_update(update)
            self._respond(200, result)
        except Exception as e:
            # Devolvemos 200 igual para que Telegram no reintente en loop;
            # el error queda en los logs de Vercel para revisar manualmente.
            self._respond(200, {"status": "error", "detail": str(e)})
