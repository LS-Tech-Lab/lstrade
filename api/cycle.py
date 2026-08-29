"""
⚠️ ESTE ARCHIVO NO SE DESPLIEGA EN VERCEL — ver app.py.

Desde 2026 el runtime Python de Vercel construye UNA sola función a partir
de un único entrypoint en la raíz (app.py) que exponga `app` (ASGI); ya no
soporta un archivo = una función por módulo dentro de api/. La ruta real
que sí está en producción es /api/cycle definido en app.py (función
cycle_get/cycle_post ahí). Este archivo queda como referencia legible de
la lógica en aislamiento, pero pegar solo esto en GitHub NO alcanza — los
cambios van en app.py.

Función serverless de Vercel — un solo ciclo de escaneo, pensado para ser
disparado por un cron externo (ver .github/workflows/trigger-cycle.yml)
cada pocos minutos, ya que el cron nativo de Vercel en el plan Hobby
solo corre una vez por día.

No espera aprobación humana (no puede — el timeout de Hobby es 10s):
si hay una señal que pasa riesgo, manda el memo por Telegram con botones
y corta. El webhook (api/telegram_webhook.py) resuelve la decisión después.
"""
from http.server import BaseHTTPRequestHandler
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
from supabase_db import SupabaseDatabase
from exchange_client import ExchangeClient
from signal_engine import compute_indicator_snapshot, generate_signal
from risk_manager import RiskManager
from trade_planner import compute_plan
from telegram_notifier import TelegramNotifier


def build_memo_markdown(symbol, signal, risk_report, plan):
    lines = [f"*MEMO DE DECISIÓN FINAL — {symbol}*"]
    lines.append(f"Señal: {signal['type']} ({signal['direction']}) — confianza {signal['confidence']}/5")
    lines.append(f"Precio: {signal['price']:.6f}")
    lines.append(f"Entrada: {plan['entry']:.6f}")
    lines.append(f"Stop loss: {plan['stop']:.6f}")
    lines.append(f"Take profit: {plan['target']:.6f}")
    lines.append(f"Ratio R:B: 1 : {plan['rr']:.2f}")
    lines.append(f"Tamaño: {plan['position_size']:.6f} unidades (~${plan['risk_amount']:.2f} de riesgo)")
    return "\n".join(lines)


def run_cycle():
    config = Config
    db = SupabaseDatabase(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    exchange_client = ExchangeClient(config)
    risk_manager = RiskManager(config, db)
    notifier = TelegramNotifier(config)

    if risk_manager.is_halted():
        reason = db.get_state("halt_reason", "desconocida")
        if db.get_state("halt_notified", "0") != "1":
            notifier.send_circuit_breaker(reason)
            db.set_state("halt_notified", "1")
        return {"status": "halted", "reason": reason}

    if db.has_open_pending_decision():
        # Ya hay un memo esperando tu respuesta en Telegram — no generamos otro encima.
        return {"status": "waiting_for_human_approval"}

    try:
        equity = exchange_client.fetch_equity() if config.LIVE_TRADING else (db.peak_equity() or 10000.0)
    except Exception as e:
        return {"status": "error", "detail": f"No se pudo obtener equity real del exchange: {e}"}

    dd_pct = risk_manager.update_equity_and_check_kill_switch(equity)

    best_signal, best_symbol = None, None
    errors = []
    for symbol in config.SYMBOLS:
        try:
            candles = exchange_client.fetch_ohlcv(symbol)
        except Exception as e:
            errors.append(f"{symbol}: {e}")
            continue

        # NUEVO: guardar el estado del mercado independiente de si hay señal
        # de trading — generate_signal() solo devuelve algo cuando pasa
        # TODOS sus filtros (la mayoría de los ciclos no), así que antes el
        # dashboard no tenía forma de mostrar "cómo está el RSI/tendencia
        # ahora mismo" fuera de esos momentos puntuales. No afecta ninguna
        # decisión de trading — es puramente para visualización.
        try:
            snapshot = compute_indicator_snapshot(candles)
            if snapshot:
                db.record_indicator_snapshot(symbol, snapshot)
        except Exception as e:
            errors.append(f"{symbol} snapshot: {e}")

        signal = generate_signal(candles)
        if signal and (best_signal is None or signal["score"] > best_signal["score"]):
            best_signal, best_symbol = signal, symbol

    if not best_signal:
        return {"status": "no_signal", "equity": equity, "drawdown_pct": dd_pct, "errors": errors}

    notifier.send_alert(f"Señal detectada: {best_symbol} · {best_signal['type']} ({best_signal['direction']})")
    try:
        ticker = exchange_client.fetch_ticker(best_symbol)
    except Exception:
        ticker = None  # el filtro de spread cae a "ok" de forma segura si no hay datos
    risk_report = risk_manager.check(best_symbol, best_signal, equity, ticker=ticker)

    if not risk_report["pass"]:
        failed = [c["label"] for c in risk_report["checks"] if not c["ok"]]
        notifier.send_message(f"\u26D4 {best_symbol} bloqueado por riesgo: {', '.join(failed)}")
        db.log_decision(best_symbol, best_signal, risk_report, None, "blocked")
        return {"status": "blocked", "symbol": best_symbol, "failed_checks": failed}

    plan = compute_plan(best_signal, risk_report, config)

    if not config.LIVE_TRADING:
        notifier.send_message(
            build_memo_markdown(best_symbol, best_signal, risk_report, plan)
            + "\n\n_(modo papel — no se ejecutó nada real)_"
        )
        db.log_decision(best_symbol, best_signal, risk_report, plan, "paper_logged")
        return {"status": "paper_logged", "symbol": best_symbol}

    if config.AUTO_EXECUTE:
        from executor import Executor
        executor = Executor(exchange_client, config)
        order_detail = executor.execute(best_symbol, plan)
        db.log_decision(best_symbol, best_signal, risk_report, plan, "auto_executed", order_detail)
        notifier.send_message(f"\u2705 Orden ejecutada automáticamente en {best_symbol}: {order_detail.get('status')}")
        return {"status": "auto_executed", "symbol": best_symbol, "order": order_detail}

    # AUTO_EXECUTE=false → aprobación humana NO bloqueante (webhook la resuelve)
    memo_md = build_memo_markdown(best_symbol, best_signal, risk_report, plan)
    message_id = notifier.send_approval_request(memo_md)
    if message_id is None:
        # Sin Telegram configurado no hay forma de pedir aprobación en serverless.
        # Por seguridad, se registra en modo papel en vez de ejecutar a ciegas.
        db.log_decision(best_symbol, best_signal, risk_report, plan, "paper_logged_no_telegram")
        return {"status": "no_telegram_configured_defaulted_to_paper", "symbol": best_symbol}

    db.create_pending_decision(message_id, best_symbol, best_signal, risk_report, plan)
    return {"status": "pending_approval", "symbol": best_symbol, "message_id": message_id}


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