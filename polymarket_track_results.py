"""
Revisa las señales de Polymarket que `polymarket_main.py` registró con plan
de salida (entrada/target/stop) y todavía no tienen resultado, trae el
precio más reciente de ese token, y si ya tocó el target o el stop lo
registra en la base — este es el paso que faltaba para poder calcular win
rate real del módulo Polymarket (antes solo existía la deduplicación de
avisos, sin ningún registro de qué pasó después).

Pensado para correr periódicamente junto al loop de polymarket_main.py
(por ejemplo, cada 30-60 min por cron o en el mismo tmux con un sleep),
no dentro del mismo ciclo — evita frenar el escaneo de mercados nuevos
esperando request de precio por cada señal vieja.

Uso:
    python polymarket_track_results.py            # un chequeo y termina
    python polymarket_track_results.py --loop 1800  # cada 30 min
"""
import argparse
import logging
import time

from config import Config
from db import Database
from polymarket_client import PolymarketClient
from telegram_notifier import TelegramNotifier

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("polymarket_track_results")


def check_open_signals(db, client, notifier):
    open_signals = db.get_open_polymarket_signals()
    if not open_signals:
        log.info("Sin señales de Polymarket pendientes de resultado.")
        return

    log.info(f"Revisando {len(open_signals)} señal(es) pendiente(s)...")
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
        log.info(f"[{outcome.upper()}] {sig['question'][:60]} ({sig['direction']})")
        if notifier.enabled:
            notifier.send_message(
                f"{emoji} *Señal Polymarket resuelta* — {sig['question'][:70]}\n"
                f"Dirección: {sig['direction']} | Resultado: {outcome.upper()}\n"
                f"Entrada: `{sig['entry']:.3f}` → Salida: `{exit_price:.3f}`"
            )
        time.sleep(0.2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", type=int, help="si se pasa, corre en loop cada N segundos en vez de una sola vez")
    args = parser.parse_args()

    config = Config
    db = Database(config.DB_PATH)
    client = PolymarketClient(config)
    notifier = TelegramNotifier(config)

    if not args.loop:
        check_open_signals(db, client, notifier)
        return

    while True:
        try:
            check_open_signals(db, client, notifier)
        except KeyboardInterrupt:
            log.info("Detenido manualmente.")
            break
        except Exception as e:
            log.exception(f"Error revisando señales: {e}")
        time.sleep(args.loop)


if __name__ == "__main__":
    main()
