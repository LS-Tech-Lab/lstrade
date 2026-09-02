"""
Entrypoint único de Vercel Functions (Python runtime 2026+).

Vercel ya no soporta un archivo = una función por cada módulo dentro de
`api/`; construye una sola Vercel Function a partir de UN entrypoint
Python en la raíz (`app.py`, `index.py`, `main.py`, etc.) que exponga
una variable `app` (ASGI/WSGI). Por eso los 9 endpoints — cycle,
manage_positions, polymarket_cycle, polymarket_resolve,
polymarket_track_results, weather_cycle, weather_track_results,
reset_halt y telegram_webhook — viven todos acá en una sola app FastAPI.
La carpeta `api/` que existía como copia de referencia de cada endpoint
por separado se eliminó del repo (nunca se desplegaba y había quedado
desincronizada de la lógica real de acá); este archivo es ahora la
única fuente de verdad.

`main.py` (loop del bot en modo VPS) sigue excluido del build por
`.vercelignore` — no es una app web y no expone `app`.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from config import Config
from supabase_db import SupabaseDatabase
from exchange_client import ExchangeClient
from signal_engine import compute_indicator_snapshot, generate_signal
from risk_manager import RiskManager, format_blocked_message
from trade_planner import compute_plan
from telegram_notifier import TelegramNotifier
from position_manager import PositionManager
from polymarket_client import PolymarketClient
from polymarket_main import SupabaseNotifyStateAdapter, run_polymarket_cycle_serverless
from polymarket_track_results import check_open_signals
from weather_signal_engine import (
    generate_weather_signal,
    build_weather_memo,
    resolve_station,
    WeatherNotifyStateStore,
)

app = FastAPI()

# Heartbeat — si pasan HEARTBEAT_INTERVAL_SECONDS sin que se mande
# ningún mensaje a Telegram (señal, bloqueo, circuit breaker, etc.), se
# manda un aviso corto de "sigo vivo" con equity/drawdown y el último
# snapshot de indicadores. Sin esto, muchas horas seguidas de "no_signal"
# se sienten indistinguibles de que el bot dejó de correr.
HEARTBEAT_INTERVAL_SECONDS = 6 * 3600

def _touch_notification(db):
    """Reinicia el reloj del heartbeat cada vez que se manda cualquier
    mensaje real a Telegram — el heartbeat solo tiene sentido cuando
    hubo silencio, no hace falta duplicar aviso el mismo ciclo."""
    db.set_state("last_notification_ts", str(time.time()))

def _maybe_send_heartbeat(db, notifier, equity, dd_pct, snapshots):
    if not notifier.enabled:
        return
    now = time.time()
    last = float(db.get_state("last_notification_ts", "0") or 0)
    if last and (now - last) < HEARTBEAT_INTERVAL_SECONDS:
        return

    hours_quiet = (now - last) / 3600 if last else None
    lines = ["🤖 *Trader IA sigue activo*"]
    if hours_quiet is not None:
        lines.append(f"Sin novedades en las últimas {hours_quiet:.1f}h — todo corriendo normal.")
    else:
        lines.append("Primer heartbeat — todo corriendo normal.")
    lines.append(f"Equity: ${equity:,.2f} | Drawdown: {dd_pct:.2f}%")
    for symbol, snap in snapshots:
        if not snap:
            continue
        lines.append(
            f"{symbol}: ${snap.get('price', 0):,.4f} · RSI {snap.get('rsi', 0):.1f} "
            f"· sesgo {snap.get('trend_bias', '—')}"
        )
    notifier.send_message("\n".join(lines))
    _touch_notification(db)

# ────────────────────────────────────────────────────────────────────
# /api/cycle — un ciclo de escaneo, disparado por cron externo
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
    position_manager = PositionManager(config, db, exchange_client, notifier)

    # NUEVO (Semana 1): Health Check del Exchange antes de iniciar cualquier ciclo.
    # Si la API del exchange está caída, en mantenimiento o la clave fue revocada,
    # es mejor abortar inmediatamente y avisar, en lugar de fallar silenciosamente.
    if config.LIVE_TRADING:
        try:
            test_symbol = config.SYMBOLS[0] if config.SYMBOLS else "BTC/USDT"
            exchange_client.fetch_ticker(test_symbol)
        except Exception as e:
            notifier.send_message(f"🚨 ALERTA CRÍTICA: Fallo de conexión con el exchange ({config.EXCHANGE_ID}). Ciclo abortado. Error: {e}")
            return {"status": "exchange_error", "detail": str(e)}

    if risk_manager.is_halted():
        reason = db.get_state("halt_reason", "desconocida")
        if db.get_state("halt_notified", "0") != "1":
            notifier.send_circuit_breaker(reason)
            db.set_state("halt_notified", "1")
            _touch_notification(db)
        return {"status": "halted", "reason": reason}

    # FIX: acá faltaba gestionar las posiciones abiertas (Trailing Stop y
    # detección de target/stop) — main.py (modo VPS) sí lo hacía, pero
    # app.py (el entrypoint que realmente corre en Vercel vía cron-job.org)
    # nunca importaba ni llamaba a PositionManager. Resultado real en la
    # base de datos: open_trades acumulaba posiciones (incluso duplicadas
    # del mismo símbolo) que nunca se cerraban, closed_trades se quedó en 0
    # filas siempre, y no había forma de calcular win rate/expectancy real
    # ni de mover el stop a breakeven/trailing en producción.
    position_manager.manage_open_positions()

    if db.has_open_pending_decision():
        expired = db.expire_stale_pending_decisions(config.PENDING_DECISION_EXPIRY_SECONDS)
        for pending in expired:
            symbol = pending.get("symbol")
            notifier.send_message(
                f"\u23F1 Decisión pendiente en {symbol} venció sin respuesta "
                f"({config.PENDING_DECISION_EXPIRY_SECONDS/60:.0f} min) — no se ejecutó nada."
            )
            db.log_decision(
                symbol, pending.get("signal"), pending.get("risk_report"),
                pending.get("plan"), "expired",
            )
        if expired:
            _touch_notification(db)

        if db.has_open_pending_decision():
            equity = exchange_client.fetch_equity() if config.LIVE_TRADING else (db.last_equity() or 10000.0)
            dd_pct = risk_manager.update_equity_and_check_kill_switch(equity)
            _maybe_send_heartbeat(db, notifier, equity, dd_pct, [])
            return {"status": "waiting_for_human_approval"}

    try:
        equity = exchange_client.fetch_equity() if config.LIVE_TRADING else (db.last_equity() or 10000.0)
    except Exception as e:
        return {"status": "error", "detail": f"No se pudo obtener equity real del exchange: {e}"}

    dd_pct = risk_manager.update_equity_and_check_kill_switch(equity)

    started = time.monotonic()
    time_budget = float(os.environ.get("CYCLE_TIME_BUDGET_SECONDS", "20.0"))

    def time_left():
        return time_budget - (time.monotonic() - started)

    best_signal, best_symbol = None, None
    errors = []
    snapshots = []
    for symbol in config.SYMBOLS:
        if time_left() < 1.0:
            errors.append(f"{symbol}: sin tiempo — se cortó el escaneo (quedaban {len(config.SYMBOLS) - config.SYMBOLS.index(symbol)} símbolo(s))")
            break
        try:
            candles = exchange_client.fetch_ohlcv(symbol)
        except Exception as e:
            errors.append(f"{symbol}: {e}")
            continue

        try:
            snapshot = compute_indicator_snapshot(candles)
            if snapshot:
                db.record_indicator_snapshot(symbol, snapshot)
                snapshots.append((symbol, snapshot))
        except Exception as e:
            errors.append(f"{symbol} snapshot: {e}")

        signal = generate_signal(candles)
        if signal and (best_signal is None or signal["score"] > best_signal["score"]):
            best_signal, best_symbol = signal, symbol

    if not best_signal:
        _maybe_send_heartbeat(db, notifier, equity, dd_pct, snapshots)
        return {"status": "no_signal", "equity": equity, "drawdown_pct": dd_pct, "errors": errors}

    notifier.send_alert(f"Señal detectada: {best_symbol} · {best_signal['type']} ({best_signal['direction']})")
    _touch_notification(db)
    try:
        ticker = exchange_client.fetch_ticker(best_symbol)
    except Exception as e:
        ticker = None
        errors.append(f"{best_symbol} ticker: {e}")
    risk_report = risk_manager.check(best_symbol, best_signal, equity, ticker=ticker)

    if not risk_report["pass"]:
        failed = [c["label"] for c in risk_report["checks"] if not c["ok"]]
        notifier.send_message(format_blocked_message(best_symbol, best_signal, failed))
        _touch_notification(db)
        db.log_decision(best_symbol, best_signal, risk_report, None, "blocked")
        return {"status": "blocked", "symbol": best_symbol, "failed_checks": failed}

    plan = compute_plan(best_signal, risk_report, config)

    if not config.LIVE_TRADING:
        notifier.send_message(
            f"\U0001F4C8 *Posición abierta (papel)* — {best_symbol}\n\n"
            + build_memo_markdown(best_symbol, best_signal, risk_report, plan)
            + "\n\n_(modo papel — no se ejecutó nada real, pero queda registrada "
              "y se va a monitorear hasta que toque target o stop)_"
        )
        _touch_notification(db)
        db.log_decision(best_symbol, best_signal, risk_report, plan, "paper_logged")
        db.add_open_trade(
            best_symbol, best_signal["direction"], plan["entry"], plan["stop"],
            plan["target"], plan["position_size"],
        )
        return {"status": "paper_logged", "symbol": best_symbol}

    if config.AUTO_EXECUTE:
        from executor import Executor
        executor = Executor(exchange_client, config)
        order_detail = executor.execute(best_symbol, plan)
        db.log_decision(best_symbol, best_signal, risk_report, plan, "auto_executed", order_detail)
        stop_order = order_detail.get("stop_order") if isinstance(order_detail, dict) else None
        order_id = (
            stop_order.get("id") if isinstance(stop_order, dict)
            else order_detail.get("order", {}).get("id") if isinstance(order_detail, dict) else None
        )
        db.add_open_trade(
            best_symbol, best_signal["direction"], plan["entry"], plan["stop"], plan["target"],
            plan["position_size"], order_id,
        )
        notifier.send_message(f"\u2705 Orden ejecutada automáticamente en {best_symbol}: {order_detail.get('status')}")
        if isinstance(order_detail, dict) and order_detail.get("stop_order_error"):
            notifier.send_message(
                f"\u26A0\uFE0F {best_symbol}: la entrada se ejecutó pero el STOP-LOSS real "
                f"NO se pudo colocar en el exchange ({order_detail['stop_order_error']}) — "
                f"posición desprotegida, revisar a mano."
            )
        _touch_notification(db)
        return {"status": "auto_executed", "symbol": best_symbol, "order": order_detail}

    memo_md = build_memo_markdown(best_symbol, best_signal, risk_report, plan)
    message_id = notifier.send_approval_request(memo_md)
    if message_id is None:
        db.log_decision(best_symbol, best_signal, risk_report, plan, "paper_logged_no_telegram")
        db.add_open_trade(
            best_symbol, best_signal["direction"], plan["entry"], plan["stop"],
            plan["target"], plan["position_size"],
        )
        return {"status": "no_telegram_configured_defaulted_to_paper", "symbol": best_symbol}

    _touch_notification(db)
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
# /api/polymarket_cycle y /api/polymarket_resolve
# ────────────────────────────────────────────────────────────────────

def run_polymarket_cycle():
    config = Config
    db = SupabaseDatabase(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    client = PolymarketClient(config)
    notifier = TelegramNotifier(config)
    state_store = SupabaseNotifyStateAdapter(
        db, resend_cooldown_hours=getattr(config, "POLYMARKET_RESEND_COOLDOWN_HOURS", 6.0)
    )
    top_n = int(os.environ.get("POLYMARKET_SERVERLESS_TOP_N", "15"))
    time_budget = float(os.environ.get("POLYMARKET_TIME_BUDGET_SECONDS", "7.5"))
    request_timeout = float(os.environ.get("POLYMARKET_REQUEST_TIMEOUT_SECONDS", "5"))
    return run_polymarket_cycle_serverless(
        config, client, notifier, db, state_store,
        top_n=top_n, time_budget_seconds=time_budget, request_timeout=request_timeout,
    )

async def _polymarket_cycle_endpoint(request: Request):
    expected = os.environ.get("CRON_SECRET")
    auth = request.headers.get("Authorization", "")
    if expected and auth != f"Bearer {expected}":
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        result = run_polymarket_cycle()
        return JSONResponse(result, status_code=200)
    except Exception as e:
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)

@app.get("/api/polymarket_cycle")
async def polymarket_cycle_get(request: Request):
    return await _polymarket_cycle_endpoint(request)

@app.post("/api/polymarket_cycle")
async def polymarket_cycle_post(request: Request):
    return await _polymarket_cycle_endpoint(request)

def run_polymarket_resolve():
    config = Config
    db = SupabaseDatabase(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    client = PolymarketClient(config)
    notifier = TelegramNotifier(config)
    open_before = db.get_open_polymarket_signals()
    check_open_signals(db, client, notifier, config)
    open_after = db.get_open_polymarket_signals()
    return {
        "status": "ok",
        "open_before": len(open_before),
        "resolved": len(open_before) - len(open_after),
        "open_after": len(open_after),
    }

async def _polymarket_resolve_endpoint(request: Request):
    expected = os.environ.get("CRON_SECRET")
    auth = request.headers.get("Authorization", "")
    if expected and auth != f"Bearer {expected}":
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        result = run_polymarket_resolve()
        return JSONResponse(result, status_code=200)
    except Exception as e:
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)

@app.get("/api/polymarket_resolve")
async def polymarket_resolve_get(request: Request):
    return await _polymarket_resolve_endpoint(request)

@app.post("/api/polymarket_resolve")
async def polymarket_resolve_post(request: Request):
    return await _polymarket_resolve_endpoint(request)
    
@app.get("/api/polymarket_history")
async def polymarket_history(request: Request):
    """Historial detallado de señales de Polymarket con métricas de rendimiento."""
    expected = os.environ.get("CRON_SECRET")
    auth = request.headers.get("Authorization", "")
    if expected and auth != f"Bearer {expected}":
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        db = SupabaseDatabase(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
        history = db.polymarket_recent_history(limit=20)
        return JSONResponse({"status": "ok", "history": history}, status_code=200)
    except Exception as e:
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)

# ────────────────────────────────────────────────────────────────────
# /api/polymarket_track_results — alias de /api/polymarket_resolve
# ────────────────────────────────────────────────────────────────────

@app.get("/api/polymarket_track_results")
async def polymarket_track_results_get(request: Request):
    return await _polymarket_resolve_endpoint(request)

@app.post("/api/polymarket_track_results")
async def polymarket_track_results_post(request: Request):
    return await _polymarket_resolve_endpoint(request)

# ────────────────────────────────────────────────────────────────────
# /api/weather_cycle
# ────────────────────────────────────────────────────────────────────

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
    # Hallazgo 01/09/2026: should_notify() permite reenviar la alerta de
    # Telegram cuando el EV sube ≥20% aunque el condition_id sea el mismo
    # de antes (tiene sentido: avisar que la oportunidad mejoró) — pero eso
    # también insertaba una fila NUEVA en weather_signals cada vez, aunque
    # fuera el mismo mercado todavía sin resolver. Como las dos filas de un
    # mismo mercado siempre resuelven igual, el win rate/Brier score las
    # contaba como dos ensayos independientes en vez de uno. Se trackean acá
    # los condition_id ya abiertos para no duplicar el registro (el reenvío
    # de Telegram sigue funcionando igual, solo no se vuelve a insertar).
    open_condition_ids = {s["condition_id"] for s in db.get_open_weather_signals()}

    started = time.monotonic()
    # Subido de 8.0 a 18.0 (01/09/2026): con WEATHER_TOP_N=5 y 5 fuentes por
    # estación (NWS×2 + Open-Meteo + METAR + TAF), 8s alcanzaba para 1-2
    # estaciones como mucho y el resto quedaba "sin_tiempo" en casi todas
    # las corridas. Con maxDuration=25 en vercel.json, 18s deja ~7s de
    # margen para el resto del handler (fetch inicial de eventos, notif. a
    # Telegram, escritura a Supabase). Si cron-job.org tiene su propio
    # timeout de request, confirmar que sea mayor a 18-20s o va a cortar la
    # conexión antes de que Vercel termine.
    time_budget = float(os.environ.get("WEATHER_TIME_BUDGET_SECONDS", "18.0"))

    def time_left():
        return time_budget - (time.monotonic() - started)

    events = client.fetch_weather_events(limit=20, time_budget_seconds=time_budget * 0.5)
    if not events:
        return {"status": "no_events"}

    top_n = int(os.environ.get("WEATHER_TOP_N", "5"))

    def _station_icao(e):
        st = resolve_station(e.get("title") or "", override_icao=getattr(config, "WEATHER_STATION_OVERRIDE", None))
        return st.get("icao") if st else None

    sorted_events = sorted(
        events,
        key=lambda e: (
            0 if _station_icao(e) else 1,
            -sum(m.get("liquidity", 0) for m in e["markets"]),
        ),
    )

    # WEATHER_PINNED_ICAO (default KMIA, pedido de LS 01/09/2026): esa
    # estación siempre entra al lote de este ciclo, aunque su liquidez no
    # alcance para colarse en el top_n por ranking normal. El resto de los
    # slots se llena con el orden de siempre (estación resuelta primero,
    # después por liquidez).
    pinned_icao = getattr(config, "WEATHER_PINNED_ICAO", None)
    if pinned_icao:
        pinned = [e for e in sorted_events if _station_icao(e) == pinned_icao]
        rest = [e for e in sorted_events if _station_icao(e) != pinned_icao]
        events = (pinned[:1] + rest)[:top_n]
    else:
        events = sorted_events[:top_n]

    sent = 0
    scanned = 0
    detail = []
    for event in events:
        if time_left() < 1.0:
            detail.append({"title": event["title"], "status": "sin_tiempo"})
            break
        scanned += 1
        try:
            signal = generate_weather_signal(
                event, config, min_ev=config.WEATHER_MIN_EV, min_price=config.WEATHER_MIN_PRICE
            )
        except Exception as e:
            detail.append({"title": event["title"], "status": "error", "error": str(e)})
            continue

        detail_entry = {"title": event["title"], "status": signal.get("status")}
        if signal.get("reason"):
            detail_entry["reason"] = signal["reason"]
        detail.append(detail_entry)
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
            if best["condition_id"] in open_condition_ids:
                detail.append({"title": event["title"], "status": "reenvio_sin_duplicar_registro"})
            else:
                try:
                    db.record_weather_signal(
                        best["condition_id"], best["question"], event["title"],
                        signal["station"].get("icao"), best["my_prob"], best["market_price"],
                        best["ev"], signal["center_estimate_f"], signal["sigma"],
                        best.get("yes_token_id"),
                    )
                    open_condition_ids.add(best["condition_id"])
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

async def _weather_cycle_endpoint(request: Request):
    expected = os.environ.get("CRON_SECRET")
    auth = request.headers.get("Authorization", "")
    if expected and auth != f"Bearer {expected}":
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        result = run_weather_cycle()
        return JSONResponse(result, status_code=200)
    except Exception as e:
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)

@app.get("/api/weather_cycle")
async def weather_cycle_get(request: Request):
    return await _weather_cycle_endpoint(request)

@app.post("/api/weather_cycle")
async def weather_cycle_post(request: Request):
    return await _weather_cycle_endpoint(request)

# ────────────────────────────────────────────────────────────────────
# /api/weather_track_results
# ────────────────────────────────────────────────────────────────────
WEATHER_RESOLVED_YES_THRESHOLD = 0.98
WEATHER_RESOLVED_NO_THRESHOLD = 0.02

def run_weather_track_results():
    """Hallazgo 01/09/2026: la versión anterior usaba fetch_price_history()
    y marcaba "resuelto" apenas el precio en vivo del token cruzaba 0.98/0.02
    — pero eso es solo el precio de mercado, no si el mercado resolvió de
    verdad. Un bucket barato (que es justo el perfil que el bot busca:
    "el mercado lo cree improbable, yo no") se queda cotizando cerca de
    cero mientras el día todavía no terminó, así que esto marcaba "no" a
    los 30-60 minutos de firmada la señal, mucho antes de que la máxima
    real del día se supiera. Resultado: 8/8 señales resueltas "no" en
    <2.5h, win rate 0% — eso no medía el modelo, medía si el precio
    seguía bajo en la primera hora (que para un long-shot pasa casi
    siempre, termine ganando o no). Fix: usar fetch_market_by_condition_id(),
    que trae el campo `closed` real de la Gamma API — solo se resuelve la
    señal cuando el mercado efectivamente cerró y liquidó."""
    config = Config
    db = SupabaseDatabase(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    client = PolymarketClient(config)

    open_signals = db.get_open_weather_signals()
    if not open_signals:
        return {"status": "no_open_signals"}

    resolved = []
    for sig in open_signals:
        condition_id = sig.get("condition_id")
        if not condition_id:
            continue
        market = client.fetch_market_by_condition_id(condition_id)
        if not market or not market.get("closed"):
            continue  # todavía no resolvió de verdad -- se revisa en el próximo ciclo

        yes_price = market.get("yes_price", 0.0)
        if yes_price >= WEATHER_RESOLVED_YES_THRESHOLD:
            outcome = "yes"
        elif yes_price <= WEATHER_RESOLVED_NO_THRESHOLD:
            outcome = "no"
        else:
            # Cerrado pero sin precio final claro en 0/1 -- no debería pasar
            # en la práctica una vez closed=True, pero por las dudas no se
            # fuerza un outcome ambiguo; se reintenta en el próximo ciclo.
            continue

        if not db.resolve_weather_signal(sig["id"], outcome):
            continue
        resolved.append({"condition_id": condition_id, "outcome": outcome})

    return {"status": "ok", "resolved": resolved, "still_open": len(open_signals) - len(resolved)}

async def _weather_track_results_endpoint(request: Request):
    expected = os.environ.get("CRON_SECRET")
    auth = request.headers.get("Authorization", "")
    if expected and auth != f"Bearer {expected}":
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        result = run_weather_track_results()
        return JSONResponse(result, status_code=200)
    except Exception as e:
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)

@app.get("/api/weather_track_results")
async def weather_track_results_get(request: Request):
    return await _weather_track_results_endpoint(request)

@app.post("/api/weather_track_results")
async def weather_track_results_post(request: Request):
    return await _weather_track_results_endpoint(request)

# ────────────────────────────────────────────────────────────────────
# /api/manage_positions
# ────────────────────────────────────────────────────────────────────

def run_manage_positions():
    config = Config
    db = SupabaseDatabase(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    exchange_client = ExchangeClient(config)
    notifier = TelegramNotifier(config)

    # NUEVO (Semana 1): Health Check antes de intentar cerrar posiciones reales.
    if config.LIVE_TRADING:
        try:
            test_symbol = config.SYMBOLS[0] if config.SYMBOLS else "BTC/USDT"
            exchange_client.fetch_ticker(test_symbol)
        except Exception as e:
            notifier.send_message(f"🚨 ALERTA CRÍTICA: Fallo de conexión con el exchange al gestionar posiciones. Error: {e}")
            return {"status": "exchange_error", "detail": str(e)}

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
            continue

        if Config.LIVE_TRADING and trade.get("order_id"):
            try:
                exchange_client.cancel_order(symbol, trade["order_id"])
            except Exception:
                pass
            side = "sell" if direction == "LONG" else "buy"
            try:
                exchange_client.create_order(symbol, side, trade["position_size"], order_type="market")
            except Exception as e:
                errors.append(f"{symbol}: no se pudo forzar el cierre a mercado tras {outcome}: {e}")
                notifier.send_message(
                    f"\u26A0\uFE0F {symbol}: {outcome} detectado pero el cierre real en el exchange "
                    f"FALLÓ ({e}) — revisar la posición a mano."
                )

        emoji = "\u2705" if outcome == "target" else "\U0001F6D1"
        r_text = f" ({r_multiple:+.2f}R)" if r_multiple is not None else ""
        notifier.send_message(
            f"{emoji} *Posición cerrada* — {symbol} {direction}\n"
            f"Resultado: {outcome.upper()}{r_text}\nSalida: `{exit_price:.6f}`"
        )
        closed.append({"symbol": symbol, "outcome": outcome, "r_multiple": r_multiple})

    return {"status": "ok", "closed": closed, "still_open": len(open_trades) - len(closed), "errors": errors}

async def _manage_positions_endpoint(request: Request):
    expected = os.environ.get("CRON_SECRET")
    auth = request.headers.get("Authorization", "")
    if expected and auth != f"Bearer {expected}":
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        result = run_manage_positions()
        return JSONResponse(result, status_code=200)
    except Exception as e:
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)

@app.get("/api/manage_positions")
async def manage_positions_get(request: Request):
    return await _manage_positions_endpoint(request)

@app.post("/api/manage_positions")
async def manage_positions_post(request: Request):
    return await _manage_positions_endpoint(request)

# ────────────────────────────────────────────────────────────────────
# /api/reset_halt
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
# /api/telegram_webhook
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
        from executor import Executor
        executor = Executor(exchange_client, config)
        order_detail = executor.execute(symbol, plan)
        notifier.answer_callback(cq["id"], "Orden ejecutada")
        notifier.send_message(f"\u2705 Orden ejecutada en {symbol}: {order_detail.get('status')}")

        if order_detail.get("status") in ("filled", "simulated"):
            stop_order = order_detail.get("stop_order")
            order_id = (
                stop_order.get("id") if isinstance(stop_order, dict)
                else order_detail.get("order", {}).get("id") if isinstance(order_detail, dict) else None
            )
            db.add_open_trade(
                symbol, signal["direction"], plan["entry"], plan["stop"],
                plan["target"], plan["position_size"], order_id,
            )
            if order_detail.get("stop_order_error"):
                notifier.send_message(
                    f"\u26A0\uFE0F {symbol}: la entrada se ejecutó pero el STOP-LOSS real "
                    f"NO se pudo colocar en el exchange ({order_detail['stop_order_error']}) — "
                    f"posición desprotegida, revisar a mano."
                )
    elif decision == "watchlist":
        notifier.answer_callback(cq["id"], "Agregado a watchlist")
    else:
        notifier.answer_callback(cq["id"], "Rechazado")

    db.log_decision(symbol, signal, risk_report, plan, decision, order_detail)
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
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=200)
