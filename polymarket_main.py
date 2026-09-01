"""
Orquestador principal para Polymarket — módulo independiente del bot de cripto.
Ahora envía tanto las señales como el Market Watch a Telegram.
"""
import argparse
import logging
import sys
import time
from config import Config
from db import Database
from polymarket_categories import categorize
from polymarket_chart import build_signal_chart
from polymarket_client import PolymarketClient
from polymarket_signal_engine import detect_inefficiency, generate_polymarket_signal
from polymarket_state import PolymarketStateStore
from telegram_notifier import TelegramNotifier

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("polymarket_main")

# NUEVO: en el loop local el Market Watch (top 5 por volumen) se mandaba en
# CADA ciclo porque no había límite de tiempo real entre corridas. En
# serverless el cron externo dispara cada ~10-15 min, así que mandarlo cada
# vez serían decenas de mensajes por día solo de este aviso — se limita a
# un máximo de una vez por MARKET_WATCH_INTERVAL_SECONDS, usando el mismo
# patrón de reloj en bot_state que el heartbeat de cripto (ver app.py).
MARKET_WATCH_INTERVAL_SECONDS = 6 * 3600  # 6 horas


def build_market_watch_text(parsed_markets, markdown=False):
    """Construye el resumen de los top 5 mercados por volumen."""
    lines = []
    title = "👁️ *MARKET WATCH — Top 5 por Volumen*" if markdown else "👁️ MARKET WATCH — Top 5 por Volumen"
    lines.append(title)
    lines.append("")
    
    sorted_markets = sorted(parsed_markets, key=lambda x: x["volume_24h"], reverse=True)[:5]
    for i, m in enumerate(sorted_markets, 1):
        q = m['question'][:55] + "..." if len(m['question']) > 55 else m['question']
        lines.append(f"{i}. {q}")
        lines.append(f"   💰 YES: ${m['yes_price']:.3f} | NO: ${m['no_price']:.3f} | 💧 Liq: ${m['liquidity']:,.0f}")
        lines.append("")
    
    return "\n".join(lines)


def build_polymarket_memo(signal, markdown=False):
    """Construye el memo de decisión para un mercado de Polymarket."""
    m = signal["market"]
    lines = []
    title = f"🎯 *SEÑAL POLYMARKET* — {m['question'][:70]}" if markdown else f"🎯 SEÑAL POLYMARKET — {m['question'][:70]}"
    lines.append(title)
    lines.append("")
    lines.append(f"📊 Dirección: {signal['direction']} (confianza {signal['confidence']}/5)")
    lines.append(f"💰 Precio YES: ${m['yes_price']:.3f} | NO: ${m['no_price']:.3f}")
    lines.append(f"📈 Volumen 24h: ${m['volume_24h']:,.2f}")
    lines.append(f"💧 Liquidez: ${m['liquidity']:,.2f}")
    
    if m.get("days_to_resolution") is not None:
        lines.append(f"⏳ Resolución en: {m['days_to_resolution']:.1f} días")
    
    if signal.get("momentum"):
        mom = signal["momentum"]
        lines.append(f"🚀 Momentum: {mom['momentum']*100:+.2f}% | Volatilidad: {mom['volatility']:.4f}")
    
    if signal.get("inefficiency"):
        ineff = signal["inefficiency"]
        lines.append(f"⚖️ Prob. implícita total: {ineff['total_implied_prob']:.3f} (ineficiencia: {ineff['inefficiency']:.3f})")

    if signal.get("trade_plan"):
        tp = signal["trade_plan"]
        lines.append("")
        lines.append(f"🎯 Plan sugerido (no hace falta esperar la resolución):")
        lines.append(f"   Entrada: ${tp['entry']:.3f} | Toma de ganancia: ${tp['target']:.3f} | Salida por pérdida: ${tp['stop']:.3f}")
    
    lines.append("")
    lines.append("🔍 Razones:")
    for r in signal.get("reasons", []):
        lines.append(f"  • {r}")
    
    lines.append("")
    lines.append(f"🆔 Condition ID: `{m['condition_id']}`")
    if m.get("url"):
        lines.append(f"🔗 {m['url']}")
    lines.append("")
    lines.append("_⚠️ MODO LECTURA — No se ejecutó ninguna operación real._")
    
    return "\n".join(lines)


def run_polymarket_cycle(config, client, notifier, state_store, db=None, top_n=None):
    """Ejecuta un ciclo de análisis de mercados de Polymarket."""
    log.info("Escaneando mercados de Polymarket...")
    
    limit = top_n or 50
    markets_raw = client.fetch_active_markets(limit=limit)
    
    if not markets_raw:
        log.warning("No se pudieron obtener mercados de Polymarket.")
        return []
    
    log.info(f"Obtenidos {len(markets_raw)} mercados. Analizando...")
    
    signals = []
    parsed_markets = []
    market_by_condition_id = {}
    price_history_by_condition_id = {}
    
    for market_raw in markets_raw:
        market = client.parse_market_for_analysis(market_raw)
        if not market:
            continue
        
        parsed_markets.append(market)
        market_by_condition_id[market["condition_id"]] = market
        
        if market["liquidity"] < 1000:
            continue

        # NUEVO: filtro por categoría — polymarket_backtest.py sobre 216
        # trades reales mostró que "Política / elecciones" tiene profit
        # factor 0.80 (pierde plata en promedio, no es ruido de muestra
        # chica con n=41), mientras que el resto de categorías con muestra
        # suficiente están parejas o positivas. Se saltea ANTES de pedir el
        # historial de precios — ahorra la llamada a la API además de no
        # mandar el aviso. Configurable por si se quiere ajustar sin tocar
        # código a medida que se junte más muestra por categoría.
        category = categorize(market["question"])
        if category in config.POLYMARKET_EXCLUDED_CATEGORIES:
            continue

        if not market.get("yes_token_id"):
            log.warning(f"Sin clobTokenId resuelto para '{market['question'][:50]}', se omite el historial de precios.")
            price_history = []
        else:
            # `interval` en /prices-history de CLOB NO es "tamaño de vela" —
            # es la VENTANA de tiempo hacia atrás (enum: max/all/1m/1w/1d/6h/1h),
            # y `fidelity` (minutos) es lo que sí controla el tamaño de cada
            # punto. interval="1h" pedía "la última 1 hora de historial" con
            # velas de 60 min → como mucho 1-2 puntos, nunca los 12+ que pide
            # analyze_probability_momentum(window=12) — por eso momentum_data
            # daba None siempre y ninguna señal pasaba jamás el filtro de
            # dirección (confirmado: 0 filas en polymarket_signals desde
            # siempre, pese a que el escaneo y el Market Watch sí funcionan).
            # interval="1d" + fidelity=60 trae ~24 puntos (uno por hora del
            # último día) — suficiente para la ventana de 12.
            price_history = client.fetch_price_history(market["yes_token_id"], interval="1d", fidelity=60)
        time.sleep(0.2)
        price_history_by_condition_id[market["condition_id"]] = price_history
        
        signal = generate_polymarket_signal(
            market, price_history,
            stop_vol_mult=config.POLYMARKET_STOP_VOL_MULT,
            target_rr=config.POLYMARKET_TARGET_RR,
        )
        if signal:
            signals.append(signal)
    
    # 🚨 NUEVO: Enviar Market Watch a Telegram (y también mostrar en consola)
    if parsed_markets:
        mw_console = build_market_watch_text(parsed_markets, markdown=False)
        print("\n" + "=" * 70)
        print(mw_console)
        print("=" * 70 + "\n")
        
        if notifier.enabled:
            mw_telegram = build_market_watch_text(parsed_markets, markdown=True)
            notifier.send_message(mw_telegram)
            time.sleep(0.5)  # Pausa para no saturar la API de Telegram
    
    signals.sort(key=lambda s: s["score"], reverse=True)
    
    if not signals:
        log.info("Sin señales de alta probabilidad en Polymarket este ciclo.")
        return []
    
    log.info(f"🎯 {len(signals)} señales detectadas en Polymarket este ciclo.")

    # Filtrar las que ya se avisaron recientemente sin cambios relevantes
    new_signals = [
        s for s in signals
        if state_store.should_notify(s["market"]["condition_id"], s["direction"], s["score"])
    ]

    if not new_signals:
        log.info("Todas las señales ya fueron notificadas recientemente sin cambios — no se reenvía nada.")
        return signals

    log.info(f"Enviando {len(new_signals[:3])} señal(es) nueva(s) o actualizada(s) a Telegram...")

    # Enviar máximo 3 señales por ciclo para no saturar
    for i, signal in enumerate(new_signals[:3], 1):
        memo_console = build_polymarket_memo(signal, markdown=False)
        print("\n" + "=" * 70)
        print(f"SEÑAL #{i}")
        print(memo_console)
        print("=" * 70)
        
        memo_telegram = build_polymarket_memo(signal, markdown=True)
        notifier.send_message(memo_telegram)
        state_store.record_notified(signal["market"]["condition_id"], signal["direction"], signal["score"])

        # NUEVO: gráfico de la curva de probabilidad con entrada/target/stop
        # marcados — antes la señal de Polymarket era pura data en texto a
        # pesar de tener un historial de precios ideal para visualizar.
        if signal.get("trade_plan"):
            history = price_history_by_condition_id.get(signal["market"]["condition_id"])
            if history and len(history) >= 5:
                try:
                    chart_png = build_signal_chart(signal, history)
                    notifier.send_photo(chart_png, caption=f"📈 {signal['market']['question'][:80]}")
                except Exception as e:
                    log.warning(f"No se pudo generar/enviar el gráfico de la señal: {e}")

        # NUEVO: registrar la señal con plan de salida para poder medir
        # después si ganó o perdió (ver polymarket_track_results.py).
        if db is not None and signal.get("trade_plan"):
            condition_id = signal["market"]["condition_id"]
            original_market = market_by_condition_id.get(condition_id, {})
            token_id = original_market.get("yes_token_id") if signal["direction"] == "YES" \
                else original_market.get("no_token_id")
            if token_id:
                tp = signal["trade_plan"]
                db.record_polymarket_signal(
                    condition_id, signal["market"]["question"], signal["direction"], token_id,
                    tp["entry"], tp["target"], tp["stop"],
                )

        time.sleep(0.5)
    
    return signals


class SupabaseNotifyStateAdapter:
    """
    Mismo interfaz que PolymarketStateStore (should_notify/record_notified),
    pero respaldado en la tabla polymarket_notify_state de Supabase en vez
    de un JSON en disco — necesario en serverless porque el filesystem no
    persiste entre invocaciones. Vive acá (no en app.py) porque
    es la pieza de pegamento entre run_polymarket_cycle_serverless (de este
    mismo módulo) y el backend de turno (Supabase); cualquier entrypoint
    que quiera correr el ciclo serverless la importa desde acá.
    """
    def __init__(self, db, resend_cooldown_hours=6.0, min_score_increase_pct=0.20):
        self.db = db
        self.resend_cooldown_hours = resend_cooldown_hours
        self.min_score_increase_pct = min_score_increase_pct

    def should_notify(self, condition_id, direction, score):
        return self.db.should_notify_polymarket(
            condition_id, direction, score,
            self.resend_cooldown_hours, self.min_score_increase_pct,
        )

    def record_notified(self, condition_id, direction, score):
        self.db.record_notified_polymarket(condition_id, direction, score)


def run_polymarket_cycle_serverless(config, client, notifier, db, state_store,
                                     top_n=15, time_budget_seconds=7.5,
                                     request_timeout=5):
    """
    Ciclo de Polymarket para correr como función serverless (Vercel Hobby:
    10s de presupuesto total). A diferencia de run_polymarket_cycle (loop
    local, sin límite de tiempo), acá:

      - Se arranca un reloj (time.monotonic) y se corta el análisis antes
        de acercarse al límite, devolviendo lo que se haya alcanzado a
        procesar — mejor una respuesta parcial que una función matada a
        mitad de camino por Vercel sin haber guardado nada.
      - La ineficiencia de precio (suma YES+NO != 1.00) es gratis: no
        necesita historial de precios, solo el snapshot que ya vino en
        fetch_active_markets. Se usa para priorizar QUÉ mercados merecen
        la llamada cara (fetch_price_history) en vez de pedirla para los
        primeros N por volumen como hace el loop local, donde el tiempo no
        es la restricción.
      - Sin sleep() entre requests: el loop local lo usa para no golpear
        la API muy seguido en un while True de larga duración; acá es una
        invocación puntual cada ~10 min, no hace falta.
      - Sin envío de gráficos (matplotlib): consume presupuesto de tiempo
        que en este modo es escaso. El memo de texto por Telegram alcanza;
        los gráficos siguen disponibles corriendo polymarket_main.py local.
    """
    started = time.monotonic()

    def time_left():
        return time_budget_seconds - (time.monotonic() - started)

    log.info("Escaneando mercados de Polymarket (modo serverless)...")
    markets_raw = client.fetch_active_markets(limit=50, timeout=request_timeout)
    if not markets_raw:
        return {"status": "no_markets"}

    parsed_markets = []
    candidates = []  # (inefficiency, market) — se ordena y se corta antes de gastar red
    for market_raw in markets_raw:
        market = client.parse_market_for_analysis(market_raw)
        if not market:
            continue
        parsed_markets.append(market)

        if market["liquidity"] < 1000:
            continue
        if market["yes_price"] <= 0 or market["no_price"] <= 0:
            continue
        category = categorize(market["question"])
        if category in config.POLYMARKET_EXCLUDED_CATEGORIES:
            continue

        ineff = detect_inefficiency(market)
        if ineff["is_extreme_trap"]:
            continue
        candidates.append((ineff["inefficiency"], market))

    candidates.sort(key=lambda c: c[0], reverse=True)
    candidates = candidates[:top_n]

    # NUEVO: Market Watch — restaura el aviso que existía en el loop local
    # (build_market_watch_text) y que se había quedado afuera de la versión
    # serverless. Se manda ANTES de gastar presupuesto de tiempo en el
    # historial de precios de los candidatos, para que no dependa de que
    # sobre tiempo al final del ciclo — es la parte más importante para
    # saber "el bot sigue vivo", así que tiene prioridad sobre el análisis.
    if parsed_markets and notifier.enabled and db is not None:
        now = time.time()
        last_watch = float(db.get_state("last_market_watch_ts", "0") or 0)
        if not last_watch or (now - last_watch) >= MARKET_WATCH_INTERVAL_SECONDS:
            try:
                notifier.send_message(build_market_watch_text(parsed_markets, markdown=True))
                db.set_state("last_market_watch_ts", str(now))
            except Exception as e:
                log.warning(f"No se pudo enviar el Market Watch: {e}")

    signals = []
    market_by_condition_id = {}
    analyzed = 0
    for _, market in candidates:
        if time_left() < 1.0:
            log.warning(
                f"Presupuesto de tiempo agotado — analizados {analyzed}/{len(candidates)} candidatos."
            )
            break
        market_by_condition_id[market["condition_id"]] = market
        if not market.get("yes_token_id"):
            price_history = []
        else:
            # Ver nota en run_polymarket_cycle (loop local) más arriba: interval
            # es una ventana de tiempo, no un tamaño de vela — "1h" dejaba
            # momentum_data en None siempre. "1d" trae ~24 puntos (1 por hora).
            price_history = client.fetch_price_history(
                market["yes_token_id"], interval="1d", fidelity=60, timeout=request_timeout
            )
        analyzed += 1

        signal = generate_polymarket_signal(
            market, price_history,
            stop_vol_mult=config.POLYMARKET_STOP_VOL_MULT,
            target_rr=config.POLYMARKET_TARGET_RR,
        )
        if signal:
            signals.append(signal)

    signals.sort(key=lambda s: s["score"], reverse=True)
    if not signals:
        return {
            "status": "no_signal",
            "markets_scanned": len(parsed_markets),
            "candidates_analyzed": analyzed,
        }

    new_signals = [
        s for s in signals
        if state_store.should_notify(s["market"]["condition_id"], s["direction"], s["score"])
    ]

    sent = 0
    for signal in new_signals[:3]:
        if time_left() < 0.5:
            log.warning("Presupuesto de tiempo agotado antes de enviar todas las señales nuevas.")
            break

        memo_telegram = build_polymarket_memo(signal, markdown=True)
        notifier.send_message(memo_telegram)
        state_store.record_notified(signal["market"]["condition_id"], signal["direction"], signal["score"])

        if db is not None and signal.get("trade_plan"):
            condition_id = signal["market"]["condition_id"]
            original_market = market_by_condition_id.get(condition_id, {})
            token_id = original_market.get("yes_token_id") if signal["direction"] == "YES" \
                else original_market.get("no_token_id")
            if token_id:
                tp = signal["trade_plan"]
                db.record_polymarket_signal(
                    condition_id, signal["market"]["question"], signal["direction"], token_id,
                    tp["entry"], tp["target"], tp["stop"],
                )
        sent += 1

    return {
        "status": "ok",
        "markets_scanned": len(parsed_markets),
        "candidates_analyzed": analyzed,
        "signals_found": len(signals),
        "signals_sent": sent,
    }


def main():
    parser = argparse.ArgumentParser(description="Trader IA para Polymarket")
    parser.add_argument("--once", action="store_true", help="corre un solo ciclo")
    parser.add_argument("--top", type=int, default=20, help="cuántos mercados analizar (default: 20)")
    parser.add_argument("--loop-interval", type=int, default=600, help="segundos entre ciclos (default: 600)")
    args = parser.parse_args()
    
    config = Config
    
    log.info("=" * 70)
    log.info("POLYMARKET ANALYZER — Modo LECTURA (sin ejecución real)")
    log.info("=" * 70)
    log.info(f"Analizando top {args.top} mercados por volumen")
    
    client = PolymarketClient(config)
    notifier = TelegramNotifier(config)
    db = Database(config.DB_PATH)
    state_store = PolymarketStateStore(
        resend_cooldown_hours=getattr(config, "POLYMARKET_RESEND_COOLDOWN_HOURS", 6.0)
    )

    telegram_status = "activado" if notifier.enabled else "desactivado"
    log.info(f"Telegram: {telegram_status}")
    
    if notifier.enabled:
        notifier.send_alert(
            f"🎰 *Polymarket Analyzer iniciado*\n"
            f"Modo: LECTURA (sin ejecución real)\n"
            f"Escaneando top {args.top} mercados"
        )
    
    if args.once:
        run_polymarket_cycle(config, client, notifier, state_store, db=db, top_n=args.top)
        return
    
    while True:
        try:
            run_polymarket_cycle(config, client, notifier, state_store, db=db, top_n=args.top)
        except KeyboardInterrupt:
            log.info("Detenido manualmente por el usuario.")
            if notifier.enabled:
                notifier.send_message("⏹️ Polymarket Analyzer detenido manualmente.")
            break
        except Exception as e:
            log.exception(f"Error inesperado en el ciclo: {e}")
            if notifier.enabled:
                notifier.send_message(f"⚠️ Error en Polymarket Analyzer: {e}")
        
        log.info(f"Próximo ciclo en {args.loop_interval} segundos...")
        time.sleep(args.loop_interval)


if __name__ == "__main__":
    main()
