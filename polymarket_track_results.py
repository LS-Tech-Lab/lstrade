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

def _safe_apply_pnl(fn, *args, **kwargs):
    """FIX (08/09/2026): ver el mismo helper en app.py -- un fallo en el
    tracking de equity por módulo (agregado 07/09/2026, migración SQL
    de `module` en equity_history no incluida en el repo) no debe poder
    abortar el resto del loop de resolución de señales de Polymarket."""
    try:
        fn(*args, **kwargs)
    except Exception as e:
        log.warning(f"[equity] no se pudo actualizar equity de {args[0] if args else '?'}: {e}")

def _safe_pnl_dollars(db, module, r_multiple, risk_pct):
    """AUDITORÍA (11/09/2026): el mensaje de Telegram mostraba el
    resultado solo en % ("Perdiste 22.5%"), pero no cuánto fue eso en
    plata real -- igual que se corrigió para cripto/clima/MLB con
    pnl_dollars. Se recalcula acá (mismo criterio que
    apply_r_multiple_pnl en supabase_db.py: base = último equity del
    módulo, o $100 si todavía no tiene historial) en vez de modificar
    esa función para que devuelva el monto, así un fallo al leer el
    equity no aborta el resto del loop -- devuelve None y el mensaje
    cae de nuevo a mostrar solo el %."""
    try:
        base = db.last_equity(module)
        if base is None:
            base = 100.0
        risk_amount = base * (risk_pct / 100.0)
        return risk_amount * r_multiple
    except Exception as e:
        log.warning(f"[equity] no se pudo calcular pnl en $ de {module}: {e}")
        return None

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
                # AUDITORÍA (07/09/2026, pedido del usuario): equity propio
                # del módulo Polymarket -- acá no hay my_prob (no es un
                # modelo de fundamentos como clima/MLB, es un plan de
                # entrada/target/stop), así que en vez de ½ Kelly se usa el
                # mismo esquema de riesgo fijo por operación que ya usa
                # risk_manager.py para cripto (RISK_PCT_PER_TRADE). Ver
                # apply_r_multiple_pnl en supabase_db.py.
                stop_distance = abs(sig["entry"] - sig["stop"])
                pnl_dollars = None
                if stop_distance > 0:
                    r_multiple = (final_price - sig["entry"]) / stop_distance
                    risk_pct = getattr(config, "RISK_PCT_PER_TRADE", 1.0)
                    pnl_dollars = _safe_pnl_dollars(db, "polymarket", r_multiple, risk_pct)
                    _safe_apply_pnl(db.apply_r_multiple_pnl, "polymarket", r_multiple, risk_pct)
                log.warning(
                    f"[CERRADO SIN STOP DETECTADO A TIEMPO] {sig['question'][:60]} "
                    f"({sig['direction']}) — el mercado ya resolvió, precio final {final_price:.3f}, "
                    f"nunca se detectó cruce de stop/target antes del cierre."
                )
                if notifier.enabled:
                    late_return_pct = ((final_price - sig["entry"]) / sig["entry"]) * 100 if sig["entry"] > 0 else None
                    pnl_txt = f" (${pnl_dollars:+.2f})" if pnl_dollars is not None else ""
                    late_profit_line = (
                        f"{'📈 Ganaste' if late_return_pct >= 0 else '📉 Perdiste'} {abs(late_return_pct):.1f}%{pnl_txt}\n"
                        f"💰 Entrada: ${sig['entry']:.3f} → Cierre: ${final_price:.3f}"
                        if late_return_pct is not None
                        else f"💰 Entrada: ${sig['entry']:.3f} → Cierre: ${final_price:.3f}"
                    )
                    notifier.send_message(
                        f"⚠️ *Señal Polymarket resuelta sin aviso previo* — {sig['question'][:70]}\n\n"
                        f"Dirección: {sig['direction']}\n"
                        f"{late_profit_line}\n\n"
                        f"El mercado ya cerró antes de que el bot detectara un cruce de stop/target.\n"
                        f"Revisar manualmente si esta posición se sostuvo hasta acá en la práctica."
                    )
            continue

        outcome = "target" if hit_target else "stop"
        exit_price = sig["target"] if hit_target else sig["stop"]
        if not db.resolve_polymarket_signal(sig["id"], exit_price, outcome):
            continue  # otra invocación ya la había resuelto

        # AUDITORÍA (07/09/2026): ver comentario equivalente más arriba
        # (rama de cierre tardío sin cruce detectado a tiempo).
        stop_distance = abs(sig["entry"] - sig["stop"])
        pnl_dollars = None
        if stop_distance > 0:
            r_multiple = (exit_price - sig["entry"]) / stop_distance
            risk_pct = getattr(config, "RISK_PCT_PER_TRADE", 1.0)
            pnl_dollars = _safe_pnl_dollars(db, "polymarket", r_multiple, risk_pct)
            _safe_apply_pnl(db.apply_r_multiple_pnl, "polymarket", r_multiple, risk_pct)

        # FIX (07/09/2026, pedido explícito del usuario): el mensaje mostraba
        # "Liquidez al cierre" siempre, pero no el beneficio real -- lo único
        # que de verdad importa una vez resuelta la señal. La liquidez del
        # book sigue siendo útil, pero solo como AVISO cuando está baja
        # (riesgo real de slippage al salir); si está bien, no se menciona.
        # Misma fórmula de retorno que ya usa el dashboard (route.js
        # computePolymarketStats) para que el número coincida con las
        # estadísticas agregadas: (salida - entrada) / entrada.
        # AUDITORÍA (11/09/2026): se agrega el $ ganado/perdido (no solo el
        # %) y se separa la línea de entrada/salida del resultado, mismo
        # criterio de claridad que se aplicó al memo de cripto.
        return_pct = ((exit_price - sig["entry"]) / sig["entry"]) * 100 if sig["entry"] > 0 else None
        if return_pct is not None:
            result_word = "📈 Ganaste" if return_pct >= 0 else "📉 Perdiste"
            pnl_txt = f" (${pnl_dollars:+.2f})" if pnl_dollars is not None else ""
            profit_line = f"{result_word} {abs(return_pct):.1f}%{pnl_txt}\n💰 Entrada: ${sig['entry']:.3f} → Salida: ${exit_price:.3f}"
        else:
            profit_line = f"💰 Entrada: ${sig['entry']:.3f} → Salida: ${exit_price:.3f}"

        current_liquidity = client.fetch_order_book_liquidity(sig["token_id"])
        liquidity_warning = (
            f"⚠️ Liquidez baja al cierre (${current_liquidity:,.0f}) — puede haber costado más "
            f"caro salir de esta posición en la práctica."
            if current_liquidity is not None and current_liquidity < min_liquidity
            else None
        )

        # Mismo criterio que el fix de Cripto: el resultado real lo dice el
        # signo del retorno, no cuál nivel (target/stop) fue el que se tocó.
        won = return_pct >= 0 if return_pct is not None else outcome == "target"
        emoji = "✅" if won else "🛑"
        outcome_txt = "tocó el target" if outcome == "target" else "tocó el stop"
        log.info(f"[{outcome.upper()}] {sig['question'][:60]} ({sig['direction']}) — {profit_line}")
        if notifier.enabled:
            message = (
                f"{emoji} *Señal Polymarket resuelta* — {sig['question'][:70]}\n\n"
                f"Dirección: {sig['direction']} ({outcome_txt})\n"
                f"{profit_line}"
            )
            if liquidity_warning:
                message += f"\n\n{liquidity_warning}"
            notifier.send_message(message)
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
