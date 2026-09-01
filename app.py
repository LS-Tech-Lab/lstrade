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

# NUEVO: heartbeat — si pasan HEARTBEAT_INTERVAL_SECONDS sin que se mande
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
# (ver .github/workflows/trigger-cycle.yml)
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
            _touch_notification(db)
        return {"status": "halted", "reason": reason}

    if db.has_open_pending_decision():
        # NUEVO: antes esto cortaba el ciclo para siempre si te perdías el
        # aviso de Telegram — ni señales nuevas, ni heartbeat, nada, hasta
        # que vos mismo tocaras un botón en un mensaje potencialmente viejo.
        # Ahora primero se vencen las que ya pasaron el timeout (se registran
        # como 'expired' y se avisa), y recién si sigue habiendo una
        # pendiente DENTRO del timeout se corta el ciclo — pero igual se
        # manda el heartbeat si hace falta, para no quedar en silencio total
        # mientras esperás.
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
            equity = exchange_client.fetch_equity() if config.LIVE_TRADING else (db.peak_equity() or 10000.0)
            dd_pct = risk_manager.update_equity_and_check_kill_switch(equity)
            _maybe_send_heartbeat(db, notifier, equity, dd_pct, [])
            return {"status": "waiting_for_human_approval"}

    try:
        equity = exchange_client.fetch_equity() if config.LIVE_TRADING else (db.peak_equity() or 10000.0)
    except Exception as e:
        return {"status": "error", "detail": f"No se pudo obtener equity real del exchange: {e}"}

    dd_pct = risk_manager.update_equity_and_check_kill_switch(equity)

    # NUEVO: presupuesto de tiempo — antes este ciclo no tenía ninguno (a
    # diferencia de weather_cycle/polymarket_cycle_serverless), así que un
    # exchange lento en un cold start (load_markets() implícito la primera
    # vez que se llama fetch_ohlcv() en el proceso, sin caché entre
    # invocaciones serverless) podía colgar la función entera hasta que
    # Vercel la mataba con FUNCTION_INVOCATION_TIMEOUT — sin heartbeat, sin
    # log de error, sin nada: silencio total. Ahora se corta el escaneo de
    # símbolos restantes si queda poco tiempo y se sigue con lo que ya se
    # tiene (best_signal encontrado hasta ese punto), igual que el resto
    # del pipeline.
    started = time.monotonic()
    time_budget = float(os.environ.get("CYCLE_TIME_BUDGET_SECONDS", "20.0"))

    def time_left():
        return time_budget - (time.monotonic() - started)

    best_signal, best_symbol = None, None
    errors = []
    snapshots = []  # [(symbol, snapshot_dict)] — se reutiliza para el heartbeat si el ciclo queda en no_signal
    for symbol in config.SYMBOLS:
        if time_left() < 1.0:
            errors.append(f"{symbol}: sin tiempo — se cortó el escaneo (quedaban {len(config.SYMBOLS) - config.SYMBOLS.index(symbol)} símbolo(s))")
            break
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
                snapshots.append((symbol, snapshot))
        except Exception as e:
            errors.append(f"{symbol} snapshot: {e}")

        signal = generate_signal(candles)
        if signal and (best_signal is None or signal["score"] > best_signal["score"]):
            best_signal, best_symbol = signal, symbol

    if not best_signal:
        # NUEVO: heartbeat — si hace HEARTBEAT_INTERVAL_SECONDS que no se manda
        # nada a Telegram, este es el punto donde más falta hace (silencio
        # largo = "¿esto sigue corriendo?"). No afecta el resultado del ciclo.
        _maybe_send_heartbeat(db, notifier, equity, dd_pct, snapshots)
        return {"status": "no_signal", "equity": equity, "drawdown_pct": dd_pct, "errors": errors}

    notifier.send_alert(f"Señal detectada: {best_symbol} · {best_signal['type']} ({best_signal['direction']})")
    _touch_notification(db)
    # NUEVO: antes se llamaba a check() sin ticker, así que el chequeo de
    # spread SIEMPRE caía en la rama "sin datos" — y esa rama decía
    # "ok": True (fail-open: sin datos, se aprueba igual). En la práctica
    # el filtro de spread nunca bloqueó nada, nunca. Ahora se trae el
    # ticker de verdad; si igual falla (red, símbolo raro), risk_manager
    # ya fue corregido para fallar CERRADO en ese caso — ver risk_manager.py.
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
        # NUEVO: antes este mensaje era el memo de decisión pelado con una
        # aclaración chiquita al final — se leía igual que un mensaje de
        # aprobación pendiente, no como confirmación de que la posición ya
        # quedó abierta y en seguimiento. Ahora tiene el mismo formato
        # "evento" (emoji + título en negrita) que los mensajes de cierre
        # (✅/🛑 Posición cerrada de /api/manage_positions más abajo), para que
        # abrir y cerrar se lean simétricos en el chat.
        notifier.send_message(
            f"\U0001F4C8 *Posición abierta (papel)* — {best_symbol}\n\n"
            + build_memo_markdown(best_symbol, best_signal, risk_report, plan)
            + "\n\n_(modo papel — no se ejecutó nada real, pero queda registrada "
              "y se va a monitorear hasta que toque target o stop)_"
        )
        _touch_notification(db)
        db.log_decision(best_symbol, best_signal, risk_report, plan, "paper_logged")
        # Sin esto la señal quedaba en el memo de Telegram y en `decisions`,
        # pero nunca en `open_trades`, así que run_manage_positions() no
        # tenía nada que cerrar ni medir.
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
        # NUEVO: se guarda el id de la orden de STOP real (no el de la
        # entrada, que ya se llenó y no necesita más gestión) — es la que
        # run_manage_positions() necesita para cancelar/reemplazar después.
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

    # AUTO_EXECUTE=false → aprobación humana NO bloqueante (webhook la resuelve)
    memo_md = build_memo_markdown(best_symbol, best_signal, risk_report, plan)
    message_id = notifier.send_approval_request(memo_md)
    if message_id is None:
        # Sin Telegram configurado no hay forma de pedir aprobación en serverless.
        # Por seguridad, se registra en modo papel en vez de ejecutar a ciegas.
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
# /api/polymarket_cycle y /api/polymarket_resolve — mismo patrón que
# /api/cycle de arriba, consolidados acá por la misma razón: Vercel solo
# construye UNA función a partir de este archivo.
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
    check_open_signals(db, client, notifier)
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


# ────────────────────────────────────────────────────────────────────
# /api/polymarket_track_results — alias de /api/polymarket_resolve.
#
# Es el mismo paso del pipeline con dos nombres (mismo check_open_signals,
# mismo criterio de target/stop) — quedó duplicado porque se escribió dos
# veces en momentos distintos. Se deja este alias en vez de unificar en
# una sola ruta para que la URL que ya tiene cargada cron-job.org
# (VERCEL_POLYMARKET_TRACK_URL) siga funcionando sin tocar la config del
# cron externo. Si en algún momento se confirma que ningún cron externo
# apunta ya a esta ruta, se puede borrar este bloque +
# trigger-polymarket-track.yml y dejar solo /api/polymarket_resolve.
# ────────────────────────────────────────────────────────────────────

@app.get("/api/polymarket_track_results")
async def polymarket_track_results_get(request: Request):
    return await _polymarket_resolve_endpoint(request)


@app.post("/api/polymarket_track_results")
async def polymarket_track_results_post(request: Request):
    return await _polymarket_resolve_endpoint(request)


# ────────────────────────────────────────────────────────────────────
# /api/weather_cycle — mismo patrón que polymarket_cycle/polymarket_resolve
# arriba. Corre el análisis de clima (weather_signal_engine.py)
# separado del ciclo de precio/momentum porque un evento de clima necesita
# varias llamadas de red secuenciales (NWS points + forecast + METAR/TAF)
# que no entran cómodas en el presupuesto de 10s del ciclo principal.
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

    started = time.monotonic()
    time_budget = float(os.environ.get("WEATHER_TIME_BUDGET_SECONDS", "8.0"))

    def time_left():
        return time_budget - (time.monotonic() - started)

    events = client.fetch_weather_events(limit=20, time_budget_seconds=time_budget * 0.5)
    if not events:
        return {"status": "no_events"}

    # NUEVO: antes se ordenaba solo por liquidez, así que en días donde los
    # eventos de clima con más volumen son de ciudades sin estación cubierta
    # (ej. Shanghai/Guangzhou/Beijing — el motor solo tiene NWS/METAR/TAF de
    # EE.UU.), el top_n se llenaba entero con eventos "no_station" y Miami o
    # Chicago (que sí están cubiertos y sí tienen mercados activos) quedaban
    # afuera del corte aunque estuvieran ahí. Se prioriza primero lo que el
    # motor puede resolver de verdad, y recién dentro de eso se ordena por
    # liquidez — así no se desperdicia el presupuesto de tiempo analizando
    # ciudades que van a terminar en no_station de todos modos.
    top_n = int(os.environ.get("WEATHER_TOP_N", "3"))
    events = sorted(
        events,
        key=lambda e: (
            0 if resolve_station(e.get("title") or "", override_icao=getattr(config, "WEATHER_STATION_OVERRIDE", None)) else 1,
            -sum(m.get("liquidity", 0) for m in e["markets"]),
        ),
    )[:top_n]

    sent = 0
    scanned = 0
    detail = []
    for event in events:
        if time_left() < 1.0:
            detail.append({"title": event["title"], "status": "sin_tiempo"})
            break
        scanned += 1
        try:
            signal = generate_weather_signal(event, config, min_ev=config.WEATHER_MIN_EV)
        except Exception as e:
            detail.append({"title": event["title"], "status": "error", "error": str(e)})
            continue

        detail.append({"title": event["title"], "status": signal.get("status")})
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
            # NUEVO: registra la probabilidad estimada para poder medir
            # después (weather_track_results) si el modelo está calibrado —
            # ver weather_signals en schema.sql.
            try:
                db.record_weather_signal(
                    best["condition_id"], best["question"], event["title"],
                    signal["station"].get("icao"), best["my_prob"], best["market_price"],
                    best["ev"], signal["center_estimate_f"], signal["sigma"],
                    best.get("yes_token_id"),
                )
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
# /api/weather_track_results — resuelve las señales de clima que
# run_weather_cycle registró (weather_signals) y todavía no tienen
# outcome. A diferencia de Polymarket (que usa target/stop de un plan de
# entrada), acá no hay plan de salida — lo que importa es si el bucket
# de temperatura terminó ganando o no. Sin un endpoint de resolución
# oficial en Gamma para esto vía API pública simple, se aproxima con el
# mismo dato que ya trae price_history: el precio del token converge a
# ~1.0 o ~0.0 cuando el mercado se resuelve, así que un umbral cerca de
# los extremos es una proxy confiable sin pegarle a ningún endpoint nuevo.
# Corre con menos frecuencia que weather_cycle — los mercados de clima se
# resuelven en horas/días, no en minutos.
# ────────────────────────────────────────────────────────────────────
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
            continue  # todavía no convergió — sigue abierta

        if not db.resolve_weather_signal(sig["id"], outcome):
            continue  # otra invocación ya la había resuelto
        resolved.append({"condition_id": sig["condition_id"], "outcome": outcome})

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
# /api/manage_positions — mismo patrón que polymarket_resolve arriba, pero NO se despliega (Vercel solo construye la función a partir
# de este archivo). Revisa las posiciones cripto abiertas en modo papel y,
# si el precio actual ya tocó el target o el stop, las cierra y calcula el
# resultado en R — sin este endpoint corriendo, run_cycle() puede seguir
# llamando a add_open_trade() pero esas posiciones nunca se cierran ni se
# reflejan en stats_summary() (win rate, expectancy).
# ────────────────────────────────────────────────────────────────────

def run_manage_positions():
    db = SupabaseDatabase(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    exchange_client = ExchangeClient(Config)
    notifier = TelegramNotifier(Config)

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
            # Otra invocación (cron solapado, reintento) ya cerró este
            # trade primero — no duplicar el aviso de Telegram ni el
            # conteo de "closed".
            continue

        # NUEVO: antes esto solo cerraba la posición real en el exchange
        # cuando outcome=="target" — si tocaba el STOP, la base de datos
        # quedaba diciendo "cerrada" pero la posición real seguía abierta y
        # expuesta, sin que el bot volviera a mirarla nunca más (ya no está
        # en open_trades). Ahora se cierra a mercado en los dos casos, igual
        # que ya se hacía para target. Para "stop" esto es además una red de
        # seguridad: si la orden de stop-loss real (creada en executor.py al
        # entrar) ya se ejecutó sola, este intento de cierre adicional va a
        # fallar solo (ej. "insufficient balance") porque ya no hay nada que
        # cerrar — se loguea en `errors`, no rompe nada.
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
# /api/reset_halt — reinicia manualmente el circuit breaker
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

        # NUEVO: antes esto nunca llegaba a open_trades — la orden se
        # ejecutaba de verdad en el exchange pero quedaba invisible para
        # run_manage_positions() para siempre, sin gestión ni cierre nunca.
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
        # Devolvemos 200 igual para que Telegram no reintente en loop;
        # el error queda en los logs de Vercel para revisar manualmente.
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=200)
