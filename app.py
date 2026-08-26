"""
Entrypoint único de Vercel Functions (Python runtime 2026+).

Vercel ya no soporta un archivo = una función por cada módulo dentro de
`api/`; construye una sola Vercel Function a partir de UN entrypoint
Python en la raíz (`app.py`, `index.py`, `main.py`, etc.) que exponga
una variable `app` (ASGI/WSGI). Por eso los 3 endpoints que antes vivían
en `api/cycle.py`, `api/reset_halt.py` y `api/telegram_webhook.py` se
consolidan acá en una sola app FastAPI, con las mismas rutas, misma
lógica de negocio, misma auth y mismas respuestas que antes.

`main.py` (loop del bot en modo VPS) sigue excluido del build por
`.vercelignore` — no es una app web y no expone `app`.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from config import Config
from supabase_db import SupabaseDatabase
from exchange_client import ExchangeClient
from signal_engine import generate_signal
from risk_manager import RiskManager
from trade_planner import compute_plan
from telegram_notifier import TelegramNotifier

app = FastAPI()


# ────────────────────────────────────────────────────────────────────
# /api/cycle — un ciclo de escaneo, disparado por cron externo
# (ver api/cycle.py original / .github/workflows/trigger-cycle.yml)
# ────────────────────────────────────────────────────────────────────

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
        signal = generate_signal(candles)
        if signal and (best_signal is None or signal["score"] > best_signal["score"]):
            best_signal, best_symbol = signal, symbol

    if not best_signal:
        return {"status": "no_signal", "equity": equity, "drawdown_pct": dd_pct, "errors": errors}

    notifier.send_alert(f"Señal detectada: {best_symbol} · {best_signal['type']} ({best_signal['direction']})")
    risk_report = risk_manager.check(best_symbol, best_signal, equity)

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


async def _cycle_endpoint(request: Request):
    expected = os.environ.get("CRON_SECRET")
    auth = request.headers.get("Authorization", "")
    if expected and auth != f"Bearer {expected}":
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        result = run_cycle()
        return JSONResponse(result, status_code=200)
    except Exception as e:
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)


@app.get("/api/cycle")
async def cycle_get(request: Request):
    return await _cycle_endpoint(request)


@app.post("/api/cycle")
async def cycle_post(request: Request):
    return await _cycle_endpoint(request)


# ────────────────────────────────────────────────────────────────────
# /api/reset_halt — reinicia manualmente el circuit breaker
# (ver api/reset_halt.py original)
# ────────────────────────────────────────────────────────────────────

@app.post("/api/reset_halt")
async def reset_halt(request: Request):
    expected = os.environ.get("CRON_SECRET")
    auth = request.headers.get("Authorization", "")
    if expected and auth != f"Bearer {expected}":
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        db = SupabaseDatabase(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
        db.set_state("trading_halted", "0")
        db.set_state("halt_reason", "")
        db.set_state("halt_notified", "0")
        return JSONResponse({"status": "reset"}, status_code=200)
    except Exception as e:
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)


# ────────────────────────────────────────────────────────────────────
# /api/telegram_webhook — resuelve tu click de Telegram (aprobar /
# watchlist / rechazar) y ejecuta la orden real si corresponde
# (ver api/telegram_webhook.py original)
# ────────────────────────────────────────────────────────────────────

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

    pending = db.get_pending_decision(message_id)
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
        from executor import Executor
        executor = Executor(exchange_client, config)
        order_detail = executor.execute(symbol, plan)
        notifier.answer_callback(cq["id"], "Orden ejecutada")
        notifier.send_message(f"\u2705 Orden ejecutada en {symbol}: {order_detail.get('status')}")
    elif decision == "watchlist":
        notifier.answer_callback(cq["id"], "Agregado a watchlist")
    else:
        notifier.answer_callback(cq["id"], "Rechazado")

    db.log_decision(symbol, signal, risk_report, plan, decision, order_detail)
    db.resolve_pending_decision(message_id)
    return {"status": "resolved", "decision": decision, "symbol": symbol}


@app.post("/api/telegram_webhook")
async def telegram_webhook(request: Request):
    secret_header = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    expected = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
    if expected and secret_header != expected:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    body = await request.body()
    try:
        import json
        update = json.loads(body or b"{}")
        result = handle_update(update)
        return JSONResponse(result, status_code=200)
    except Exception as e:
        # Devolvemos 200 igual para que Telegram no reintente en loop;
        # el error queda en los logs de Vercel para revisar manualmente.
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=200)
