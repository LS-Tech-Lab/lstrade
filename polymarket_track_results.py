"""
Revisa las señales de Polymarket que polymarket_main.py registró con plan
de salida (entrada/target/stop) y todavía no tienen resultado.
Semana 3: Agrega validación de liquidez para evitar falsos positivos por slippage.
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

def check_open_signals(db, client, notifier, config):
    open_signals = db.get_open_polymarket_signals()
    if not open_signals:
        log.info("Sin señales de Polymarket pendientes de resultado.")
        return

    log.info(f"Revisando {len(open_signals)} señal(es) pendiente(s)...")
    min_liquidity = getattr(config, "POLYMARKET_MIN_EXIT_LIQUIDITY", 500.0)
    
    for sig in open_signals:
        # FIX (02/09/2026): antes se pedía `liquidity` a fetch_market_by_condition_id()
        # (Gamma) — que nunca traía el mercado real (ver nota en polymarket_client.py) y
        # por eso "if not market: continue" se disparaba siempre, bloqueando la resolución
        # de las 70 señales abiertas antes de que llegaran a chequear el precio. Se
        # reemplaza por la liquidez real del order book del token vía CLOB, que además es
        # más precisa que el agregado de Gamma.
        current_liquidity = client.fetch_order_book_liquidity(sig["token_id"])
        if current_liquidity is None:
            log.warning(f"[SIN DATOS] No se pudo obtener el order book para {sig['question'][:60]}")
            continue

        if current_liquidity < min_liquidity:
            log.warning(f"[LIQUIDEZ BAJA] {sig['question'][:60]} - Liquidez: ${current_liquidity:.0f} < ${min_liquidity:.0f}. Se pospone resolución.")
            continue

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
        if not db.resolve_polymarket_signal(sig["id"], exit_price, outcome):
            continue  # otra invocación ya la había resuelto

        emoji = "✅" if outcome == "target" else "🛑"
        log.info(f"[{outcome.upper()}] {sig['question'][:60]} ({sig['direction']})")
        if notifier.enabled:
            notifier.send_message(
                f"{emoji} *Señal Polymarket resuelta* — {sig['question'][:70]}\n"
                f"Dirección: {sig['direction']} | Resultado: {outcome.upper()}\n"
                f"Entrada: `{sig['entry']:.3f}` → Salida: `{exit_price:.3f}`\n"
                f"Liquidez al cierre: `${current_liquidity:,.0f}`"
            )
        time.sleep(0.2)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", type=int, help="si se pasa, corre en loop cada N segundos")
    args = parser.parse_args()

    config = Config
    db = Database(config.DB_PATH)
    client = PolymarketClient(config)
    notifier = TelegramNotifier(config)

    if not args.loop:
        check_open_signals(db, client, notifier, config)
        return

    while True:
        try:
            check_open_signals(db, client, notifier, config)
        except KeyboardInterrupt:
            log.info("Detenido manualmente.")
            break
        except Exception as e:
            log.exception(f"Error revisando señales: {e}")
        time.sleep(args.loop)

if __name__ == "__main__":
    main()
