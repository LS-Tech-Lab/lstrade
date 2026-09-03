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
        # AUDITORÍA (03/09/2026, usuario reportó señales con pérdidas de
        # 60% hasta 100%): antes el chequeo de liquidez del order book iba
        # PRIMERO y, si estaba baja, hacía `continue` sin siquiera mirar el
        # historial de precios -- es decir, ni se evaluaba si ya había
        # tocado stop o target. El problema es que la liquidez de un
        # mercado binario típicamente se seca justo cuando el precio se
        # acerca a 0 o a 1 (resolución cerca), que es EXACTAMENTE cuando
        # más urgente es detectar que el stop ya voló. El resultado: la
        # señal quedaba "pospuesta" ciclo tras ciclo mientras el precio
        # real seguía cayendo, hasta terminar en 100% sin que nunca se
        # mandara el aviso de stop.
        #
        # Ahora el chequeo de precio/stop/target va SIEMPRE primero, sin
        # depender de la liquidez -- la liquidez del book sólo se usa
        # como dato informativo en el mensaje de salida (para que el
        # usuario sepa si puede haber slippage al ejecutar la salida real
        # en Polymarket), nunca como gate que bloquee la detección.
        # FIX (03/09/2026): interval="1h" traía como mucho 1 punto (o 0 si el
        # token no tuvo ningún trade en la última hora exacta, típico de un
        # favorito perdedor ya cerca de 0) -- mismo bug de semántica de
        # `interval` diagnosticado y arreglado el 31/08 en polymarket_main.py
        # (interval es la VENTANA hacia atrás, no el tamaño de vela), pero
        # ese fix no había tocado este call site. Con history=[] acá,
        # current_price quedaba en None para siempre y la señal nunca
        # detectaba el cruce de stop -- quedaba esperando el fallback de
        # "mercado cerrado" más abajo, que en Polymarket puede tardar horas
        # por el período de disputa del oráculo UMA (caso real: señal Porto
        # id=43, 43h para resolver via ese fallback en vez de segundos via
        # cruce de precio). interval="1d" trae ~24 puntos (uno por hora del
        # último día) incluso para tokens ilíquidos, así current_price casi
        # nunca es None salvo que el token no tenga NINGÚN trade en 24h.
        history = client.fetch_price_history(sig["token_id"], interval="1d", fidelity=60)
        current_price = history[-1]["p"] if history else None

        hit_target = current_price is not None and current_price >= sig["target"]
        hit_stop = current_price is not None and current_price <= sig["stop"]

        if not (hit_target or hit_stop):
            # Todavía no tocó ni target ni stop según el historial de
            # precios. Antes de darlo por "sin novedad", chequear si el
            # mercado subyacente ya CERRÓ del todo (settlement real) --
            # eso puede pasar sin que el historial de precios muestre un
            # cruce limpio por el stop si los datos saltan directo a 0/1.
            # Sin esto, una señal así queda abierta para siempre en la DB,
            # nunca se resuelve ni se avisa, y el usuario se entera del
            # 100% de pérdida mirando Polymarket directamente, no por el
            # bot.
            clob_market = client.fetch_clob_market(sig["condition_id"])
            if clob_market and clob_market.get("closed"):
                final_yes = clob_market["yes_price"]
                final_price = final_yes if sig["direction"] == "YES" else (1.0 - final_yes)
                # Se usa "target"/"stop" (no una tercera etiqueta) para que
                # polymarket_stats_summary/el dashboard sigan contando esto
                # como win/loss real -- lo único distinto es que se detectó
                # al cerrar el mercado en vez de por un cruce de precio a
                # tiempo, y eso se deja explícito en el aviso de Telegram.
                late_outcome = "target" if final_price >= sig["entry"] else "stop"
                if not db.resolve_polymarket_signal(sig["id"], final_price, late_outcome):
                    continue
                log.warning(
                    f"[CERRADO SIN STOP DETECTADO A TIEMPO] {sig['question'][:60]} "
                    f"({sig['direction']}) — el mercado ya resolvió, precio final {final_price:.3f}, "
                    f"nunca se detectó cruce de stop/target antes del cierre."
                )
                if notifier.enabled:
                    notifier.send_message(
                        f"⚠️ *Señal Polymarket resuelta sin aviso previo* — {sig['question'][:70]}\n"
                        f"Dirección: {sig['direction']} | El mercado ya cerró antes de cruzar stop/target.\n"
                        f"Entrada: `{sig['entry']:.3f}` → Cierre: `{final_price:.3f}`\n"
                        f"Revisar manualmente si esta posición se sostuvo hasta acá en la práctica."
                    )
            continue

        outcome = "target" if hit_target else "stop"
        exit_price = sig["target"] if hit_target else sig["stop"]
        if not db.resolve_polymarket_signal(sig["id"], exit_price, outcome):
            continue  # otra invocación ya la había resuelto

        # Liquidez sólo como dato informativo en el mensaje, no como gate.
        current_liquidity = client.fetch_order_book_liquidity(sig["token_id"])
        liquidity_note = (
            f"Liquidez al cierre: `${current_liquidity:,.0f}`" if current_liquidity is not None
            else "Liquidez al cierre: sin datos"
        )
        if current_liquidity is not None and current_liquidity < min_liquidity:
            liquidity_note += " ⚠️ liquidez baja — puede haber slippage al salir en Polymarket."

        emoji = "✅" if outcome == "target" else "🛑"
        log.info(f"[{outcome.upper()}] {sig['question'][:60]} ({sig['direction']})")
        if notifier.enabled:
            notifier.send_message(
                f"{emoji} *Señal Polymarket resuelta* — {sig['question'][:70]}\n"
                f"Dirección: {sig['direction']} | Resultado: {outcome.upper()}\n"
                f"Entrada: `{sig['entry']:.3f}` → Salida: `{exit_price:.3f}`\n"
                f"{liquidity_note}"
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
