"""
Trader IA 24/7 — orquestador principal.

Uso:
    python main.py                 # corre el loop indefinidamente
    python main.py --once          # corre un solo ciclo (útil para probar)
    python main.py --reset-halt    # reinicia manualmente el circuit breaker
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("main")


def build_memo_text(symbol, signal, risk_report, plan, markdown=False):
    lines = []
    title = f"MEMO DE DECISIÓN FINAL — {symbol}"
    lines.append(f"*{title}*" if markdown else title)
    lines.append(f"Señal: {signal['type']} ({signal['direction']}) — confianza {signal['confidence']}/5")
    lines.append(f"Precio: {signal['price']:.6f}")
    if plan:
        lines.append(f"Entrada: {plan['entry']:.6f}")
        lines.append(f"Stop loss: {plan['stop']:.6f}")
        lines.append(f"Take profit: {plan['target']:.6f}")
        lines.append(f"Ratio R:B: 1 : {plan['rr']:.2f}")
        lines.append(f"Tamaño: {plan['position_size']:.6f} unidades (~${plan['risk_amount']:.2f} de riesgo)")
    lines.append("—" * 20)
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
    if resp == "s":
        return "approved"
    if resp == "w":
        return "watchlist"
    return "rejected"


def notify_halt_once(db, notifier):
    """Manda la alerta de circuit breaker por Telegram una sola vez, no en cada ciclo."""
    if db.get_state("halt_notified", "0") == "1":
        return
    reason = db.get_state("halt_reason", "desconocida")
    notifier.send_circuit_breaker(reason)
    db.set_state("halt_notified", "1")


def run_cycle(config, db, exchange_client, risk_manager, executor, notifier):
    if risk_manager.is_halted():
        reason = db.get_state("halt_reason", "desconocida")
        log.error(f"Sistema detenido por circuit breaker ({reason}). Usá --reset-halt tras revisar manualmente.")
        notify_halt_once(db, notifier)
        return

    try:
        equity = exchange_client.fetch_equity() if config.LIVE_TRADING else db.peak_equity() or 10000.0
    except Exception as e:
        log.exception(f"No se pudo obtener el balance real del exchange: {e}")
        return

    dd_pct = risk_manager.update_equity_and_check_kill_switch(equity)
    log.info(f"Equity actual: {equity:.2f} | Drawdown: {dd_pct:.2f}%")

    best_signal, best_symbol = None, None
    for symbol in config.SYMBOLS:
        try:
            candles = exchange_client.fetch_ohlcv(symbol)
        except Exception as e:
            log.warning(f"No se pudo traer OHLCV de {symbol}: {e}")
            continue
        signal = generate_signal(candles)
        if signal and (best_signal is None or signal["score"] > best_signal["score"]):
            best_signal, best_symbol = signal, symbol

    if not best_signal:
        log.info("Sin señales de alta probabilidad este ciclo. Sigo escaneando.")
        return

    log.info(f"Señal detectada en {best_symbol}: {best_signal['type']} ({best_signal['direction']})")
    notifier.send_alert(f"Señal detectada: {best_symbol} · {best_signal['type']} ({best_signal['direction']})")

    risk_report = risk_manager.check(best_symbol, best_signal, equity)

    if not risk_report["pass"]:
        log.warning(f"{best_symbol}: bloqueado por módulo de riesgo.")
        failed = [c["label"] for c in risk_report["checks"] if not c["ok"]]
        notifier.send_message(f"\u26D4 {best_symbol} bloqueado por riesgo: {', '.join(failed)}")
        db.log_decision(best_symbol, best_signal, risk_report, None, "blocked")
        return

    plan = compute_plan(best_signal, risk_report, config)
    print_memo(best_symbol, best_signal, risk_report, plan)

    if not config.LIVE_TRADING:
        notifier.send_message(build_memo_text(best_symbol, best_signal, risk_report, plan, markdown=True) +
                               "\n\n_(modo papel — no se ejecutó nada real)_")
        db.log_decision(best_symbol, best_signal, risk_report, plan, "paper_logged")
        log.info("LIVE_TRADING=false → quedó registrado en modo papel, no se ejecutó nada real.")
        return

    if config.AUTO_EXECUTE:
        decision = "auto_executed"
    else:
        memo_md = build_memo_text(best_symbol, best_signal, risk_report, plan, markdown=True)
        decision = notifier.ask_approval(memo_md)  # None si Telegram no está configurado
        if decision is None:
            decision = ask_human_confirmation(best_symbol)  # fallback: consola

    order_detail = None
    if decision in ("approved", "auto_executed"):
        order_detail = executor.execute(best_symbol, plan)
        log.info(f"Resultado de la orden: {order_detail}")
        notifier.send_message(f"\u2705 Orden ejecutada en {best_symbol}: {order_detail.get('status')}")
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

    if args.reset_halt:
        risk_manager.manual_reset()
        db.set_state("halt_notified", "0")
        log.info("Circuit breaker reiniciado manualmente.")
        notifier.send_message("\u2705 Circuit breaker reiniciado manualmente. El sistema vuelve a operar.")
        return

    mode = "LIVE (dinero real)" if config.LIVE_TRADING else "PAPER (simulado)"
    exec_mode = "automática" if config.AUTO_EXECUTE else "con confirmación humana"
    telegram_status = "activado" if notifier.enabled else "desactivado"
    log.info(f"Trader IA 24/7 iniciado — modo: {mode} | ejecución: {exec_mode} | "
             f"Telegram: {telegram_status} | símbolos: {config.SYMBOLS}")
    notifier.send_alert(f"Trader IA 24/7 iniciado — modo {mode}, ejecución {exec_mode}.")

    if args.once:
        run_cycle(config, db, exchange_client, risk_manager, executor, notifier)
        return

    while True:
        try:
            run_cycle(config, db, exchange_client, risk_manager, executor, notifier)
        except KeyboardInterrupt:
            log.info("Detenido manualmente por el usuario.")
            break
        except Exception as e:
            log.exception(f"Error inesperado en el ciclo: {e}")
            notifier.send_message(f"\u26A0\uFE0F Error inesperado en el ciclo: {e}")
        time.sleep(config.LOOP_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
