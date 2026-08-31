"""
Trader IA 24/7 — orquestador principal MEJORADO.
"""
import argparse
import logging
import sys
import time
from config import Config
from db import Database
from exchange_client import ExchangeClient
from signal_engine import generate_signal
from risk_manager import RiskManager
from trade_planner import compute_plan
from executor import Executor
from telegram_notifier import TelegramNotifier
from position_manager import PositionManager  # NUEVO

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("main")

def build_memo_text(symbol, signal, risk_report, plan, markdown=False):
    lines = []
    title = f"MEMO DE DECISIÓN FINAL — {symbol}"
    lines.append(f"**{title}**" if markdown else title)
    lines.append(f"Señal: {signal['type']} ({signal['direction']}) — confianza {signal['confidence']}/5")
    lines.append(f"Precio: {signal['price']:.6f}")
    if plan:
        lines.append(f"Entrada: {plan['entry']:.6f}")
        lines.append(f"Stop loss: {plan['stop']:.6f}")
        lines.append(f"Take profit: {plan['target']:.6f}")
        lines.append(f"Ratio R:B: 1 : {plan['rr']:.2f}")
        lines.append(f"Tamaño: {plan['position_size']:.6f} unidades (~${plan['risk_amount']:.2f} de riesgo)")
    lines.append("—" * 20)
    lines.append(f"RSI: {signal['rsi']:.1f} | Volumen: {signal.get('volume_ratio', 0):.2f}x promedio")
    for c in risk_report["checks"]:
        tag = "OK" if c["ok"] else "FALLA"
        lines.append(f"[{tag}] {c['label']}")
    return "\n".join(lines)

def print_memo(symbol, signal, risk_report, plan):
    print("\n" + "=" * 60)
    print(build_memo_text(symbol, signal, risk_report, plan))
    print("=" * 60)

def ask_human_confirmation(symbol):
    resp = input(f"\n¿Ejecutar esta operación REAL en {symbol}? [s/N/w=watchlist]: ").strip().lower()
    if resp == "s": return "approved"
    if resp == "w": return "watchlist"
    return "rejected"

def notify_halt_once(db, notifier):
    if db.get_state("halt_notified", "0") == "1": return
    reason = db.get_state("halt_reason", "desconocida")
    notifier.send_circuit_breaker(reason)
    db.set_state("halt_notified", "1")

def run_cycle(config, db, exchange_client, risk_manager, executor, notifier, position_manager):
    if risk_manager.is_halted():
        reason = db.get_state("halt_reason", "desconocida")
        log.error(f"Sistema detenido por circuit breaker ({reason}). Usá --reset-halt tras revisar manualmente.")
        notify_halt_once(db, notifier)
        return

    # 1. GESTIONAR POSICIONES ABIERTAS (Trailing Stop)
    position_manager.manage_open_positions()

    try:
        equity = exchange_client.fetch_equity() if config.LIVE_TRADING else db.peak_equity() or 10000.0
    except Exception as e:
        log.exception(f"No se pudo obtener el balance real del exchange: {e}")
        return

    dd_pct = risk_manager.update_equity_and_check_kill_switch(equity)
    log.info(f"Equity actual: {equity:.2f} | Drawdown: {dd_pct:.2f}%")

    # 2. CALCULAR BTC BIAS (Una sola vez por ciclo)
    btc_bias = None
    try:
        btc_candles = exchange_client.fetch_ohlcv("BTC/USDT", timeframe="4h", limit=50)
        if btc_candles and len(btc_candles) >= 6:
            btc_momentum = (btc_candles[-1]["c"] - btc_candles[-6]["c"]) / btc_candles[-6]["c"]
            if btc_momentum > 0.015:
                btc_bias = {"direction": "LONG"}
            elif btc_momentum < -0.015:
                btc_bias = {"direction": "SHORT"}
            else:
                btc_bias = {"direction": "NEUTRAL"}
            log.info(f"BTC bias (4H): {btc_bias['direction']} (momentum: {btc_momentum*100:.2f}%)")
    except Exception as e:
        log.warning(f"No se pudo calcular BTC bias: {e}")

    best_signal, best_symbol = None, None

    # 3. ESCANEAR SÍMBOLOS CON MTF Y SPREAD
    for symbol in config.SYMBOLS:
        try:
            candles = exchange_client.fetch_ohlcv(symbol)
            # Obtener velas de 4H para el filtro MTF
            higher_tf_candles = exchange_client.fetch_ohlcv(symbol, timeframe="4h", limit=50)
            # Obtener ticker para el filtro de spread
            ticker = exchange_client.fetch_ticker(symbol)
        except Exception as e:
            log.warning(f"No se pudo traer datos de {symbol}: {e}")
            continue

        signal = generate_signal(candles, higher_tf_candles=higher_tf_candles, btc_bias=btc_bias)
        if signal:
            risk_report = risk_manager.check(symbol, signal, equity, ticker=ticker)
            if risk_report["pass"] and (best_signal is None or signal["score"] > best_signal["score"]):
                best_signal, best_symbol = signal, symbol

    if not best_signal:
        log.info("Sin señales de alta probabilidad este ciclo. Sigo escaneando.")
        return

    log.info(f"Señal detectada en {best_symbol}: {best_signal['type']} ({best_signal['direction']})")
    notifier.send_alert(f"Señal detectada: {best_symbol} · {best_signal['type']} ({best_signal['direction']})")

    risk_report = risk_manager.check(best_symbol, best_signal, equity, ticker=exchange_client.fetch_ticker(best_symbol))
    if not risk_report["pass"]:
        log.warning(f"{best_symbol}: bloqueado por módulo de riesgo.")
        failed = [c["label"] for c in risk_report["checks"] if not c["ok"]]
        notifier.send_message(f"⛔ {best_symbol} bloqueado por riesgo: {', '.join(failed)}")
        db.log_decision(best_symbol, best_signal, risk_report, None, "blocked")
        return

    plan = compute_plan(best_signal, risk_report, config)
    print_memo(best_symbol, best_signal, risk_report, plan)

    if not config.LIVE_TRADING:
        notifier.send_message(build_memo_text(best_symbol, best_signal, risk_report, plan, markdown=True) +
                              "\n\n_(modo papel — no se ejecutó nada real)_")
        db.log_decision(best_symbol, best_signal, risk_report, plan, "paper_logged")
        # Simular apertura de trade para que el Trailing Stop funcione en papel
        db.add_open_trade(best_symbol, best_signal['direction'], plan['entry'], plan['stop'], plan['target'], plan['position_size'])
        log.info("LIVE_TRADING=false → registrado en modo papel.")
        return

    if config.AUTO_EXECUTE:
        decision = "auto_executed"
    else:
        memo_md = build_memo_text(best_symbol, best_signal, risk_report, plan, markdown=True)
        decision = notifier.ask_approval(memo_md)
        if decision is None:
            decision = ask_human_confirmation(best_symbol)

    order_detail = None
    if decision in ("approved", "auto_executed"):
        order_detail = executor.execute(best_symbol, plan)
        log.info(f"Resultado de la orden: {order_detail}")
        notifier.send_message(f"✅ Orden ejecutada en {best_symbol}: {order_detail.get('status')}")

        # NUEVO: executor.execute() ahora coloca el stop-loss real en el
        # exchange internamente (ver executor.py) — antes ese paso vivía acá
        # duplicado Y con un bug propio: guardaba el order_id de la ENTRADA
        # (ya llenada, sin uso futuro) en vez del de la orden de STOP, así
        # que position_manager.py más tarde intentaba cancelar/reemplazar la
        # orden equivocada. Ahora se usa el id que devuelve el stop real.
        stop_order = order_detail.get("stop_order") if isinstance(order_detail, dict) else None
        order_id = (
            stop_order.get("id") if isinstance(stop_order, dict)
            else order_detail.get("order", {}).get("id") if isinstance(order_detail, dict) else None
        )
        db.add_open_trade(best_symbol, best_signal['direction'], plan['entry'], plan['stop'], plan['target'], plan['position_size'], order_id)

        if isinstance(order_detail, dict) and order_detail.get("stop_order_error"):
            notifier.send_message(
                f"⚠️ {best_symbol}: la entrada se ejecutó pero el STOP-LOSS real "
                f"NO se pudo colocar en el exchange ({order_detail['stop_order_error']}) — "
                f"posición desprotegida, revisar a mano."
            )
    else:
        notifier.send_message(f"Decisión final para {best_symbol}: {decision}")
    
    db.log_decision(best_symbol, best_signal, risk_report, plan, decision, order_detail)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="corre un solo ciclo y termina")
    parser.add_argument("--reset-halt", action="store_true", help="reinicia el circuit breaker manualmente")
    args = parser.parse_args()

    problems = Config.validate()
    if problems:
        for p in problems:
            log.error(f"Config inválida: {p}")
        sys.exit(1)

    config = Config
    db = Database(config.DB_PATH)
    exchange_client = ExchangeClient(config)
    risk_manager = RiskManager(config, db)
    executor = Executor(exchange_client, config)
    notifier = TelegramNotifier(config)
    position_manager = PositionManager(config, db, exchange_client, notifier) # NUEVO

    if args.reset_halt:
        risk_manager.manual_reset()
        db.set_state("halt_notified", "0")
        log.info("Circuit breaker reiniciado manualmente.")
        notifier.send_message("✅ Circuit breaker reiniciado manualmente. El sistema vuelve a operar.")
        return

    mode = "LIVE (dinero real)" if config.LIVE_TRADING else "PAPER (simulado)"
    exec_mode = "automática" if config.AUTO_EXECUTE else "con confirmación humana"
    telegram_status = "activado" if notifier.enabled else "desactivado"
    
    log.info(f"Trader IA 24/7 iniciado — modo: {mode} | ejecución: {exec_mode} | Telegram: {telegram_status} | símbolos: {config.SYMBOLS}")
    notifier.send_alert(f"Trader IA 24/7 iniciado — modo {mode}, ejecución {exec_mode}.")

    if args.once:
        run_cycle(config, db, exchange_client, risk_manager, executor, notifier, position_manager)
        return

    while True:
        try:
            run_cycle(config, db, exchange_client, risk_manager, executor, notifier, position_manager)
        except KeyboardInterrupt:
            log.info("Detenido manualmente por el usuario.")
            break
        except Exception as e:
            log.exception(f"Error inesperado en el ciclo: {e}")
            notifier.send_message(f"⚠️ Error inesperado en el ciclo: {e}")
        time.sleep(config.LOOP_INTERVAL_SECONDS)

if __name__ == "__main__":
    main()