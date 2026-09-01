"""
Orquestador principal para Polymarket — módulo independiente del bot de cripto.
Ahora envía tanto las señales como el Market Watch a Telegram.
"""
import argparse
import logging
import sys
import time
import concurrent.futures  # NUEVO (Semana 2): Para concurrencia en descargas de red
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
    """Ejecuta un ciclo de análisis de mercados de Polymarket (modo local/VPS)."""
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

        category = categorize(market["question"])
        if category in config.POLYMARKET_EXCLUDED_CATEGORIES:
            continue

        if not market.get("yes_token_id"):
            log.warning(f"Sin clobTokenId resuelto para '{market['question'][:50]}', se omite el historial de precios.")
            price_history = []
        else:
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
    
    if parsed_markets:
        mw_console = build_market_watch_text(parsed_markets, markdown=False)
        print("\n" + "=" * 70)
        print(mw_console)
        print("=" * 70 + "\n")
        
        if notifier.enabled:
            mw_telegram = build_market_watch_text(parsed_markets, markdown=True)
            notifier.send_message(mw_telegram)
            time.sleep(0.5)
    
    signals.sort(key=lambda s: s["score"], reverse=True)
    
    if not signals:
        log.info("Sin señales de alta probabilidad en Polymarket este ciclo.")
        return []
    
    log.info(f"🎯 {len(signals)} señales detectadas en Polymarket este ciclo.")

    new_signals = [
        s for s in signals
        if state_store.should_notify(s["market"]["condition_id"], s["direction"], s["score"])
    ]

    if not new_signals:
        log.info("Todas las señales ya fueron notificadas recientemente sin cambios — no se reenvía nada.")
        return signals

    log.info(f"Enviando {len(new_signals[:3])} señal(es) nueva(s) o actualizada(s) a Telegram...")

    for i, signal in enumerate(new_signals[:3], 1):
        memo_console = build_polymarket_memo(signal, markdown=False)
        print("\n" + "=" * 70)
        print(f"SEÑAL #{i}")
        print(memo_console)
        print("=" * 70)
        
        memo_telegram = build_polymarket_memo(signal, markdown=True)
        notifier.send_message(memo_telegram)
        state_store.record_notified(signal["market"]["condition_id"], signal["direction"], signal["score"])

        if signal.get("trade_plan"):
            history = price_history_by_condition_id.get(signal["market"]["condition_id"])
            if history and len(history) >= 5:
                try:
                    chart_png = build_signal_chart(signal, history)
                    notifier.send_photo(chart_png, caption=f"📈 {signal['market']['question'][:80]}")
                except Exception as e:
                    log.warning(f"No se pudo generar/enviar el gráfico de la señal: {e}")

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
    pero respaldado en Supabase en vez de un JSON en disco — necesario en 
    serverless porque el filesystem no persiste entre invocaciones.
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
    Ciclo de Polymarket para correr como función serverless (Vercel Hobby).
    Actualizado con concurrencia (Semana 2) para descargar historiales de precios
    en paralelo y evitar el timeout de 25s de Vercel.
    """
    started = time.monotonic()

    def time_left():
        return time_budget_seconds - (time.monotonic() - started)

    log.info("Escaneando mercados de Polymarket (modo serverless)...")
    markets_raw = client.fetch_active_markets(limit=50, timeout=request_timeout)
    if not markets_raw:
        return {"status": "no_markets"}

    parsed_markets = []
    candidates = []
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
    
    # NUEVO (Semana 2): Concurrencia para descarga de historiales de precios.
    # Antes se hacía en secuencia (14 candidatos * ~1.5s = 21s), acercando
    # peligrosamente el ciclo al límite de 25s de Vercel. Con ThreadPoolExecutor,
    # las llamadas de red se ejecutan en paralelo, reduciendo el tiempo a ~2-3s.
    def fetch_history_for_market(market):
        if not market.get("yes_token_id"):
            return market["condition_id"], []
        try:
            history = client.fetch_price_history(
                market["yes_token_id"], interval="1d", fidelity=60, timeout=request_timeout
            )
            return market["condition_id"], history
        except Exception as e:
            log.warning(f"Error fetching history for {market['condition_id']}: {e}")
            return market["condition_id"], []

    history_results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        future_to_market = {
            executor.submit(fetch_history_for_market, market): market
            for _, market in candidates
        }
        for future in concurrent.futures.as_completed(future_to_market):
            if time_left() < 1.0:
                log.warning("Presupuesto de tiempo agotado durante la descarga de historiales.")
                break
            condition_id, price_history = future.result()
            history_results[condition_id] = price_history

    for _, market in candidates:
        if time_left() < 1.0:
            log.warning(
                f"Presupuesto de tiempo agotado — analizados {analyzed}/{len(candidates)} candidatos."
            )
            break
        
        market_by_condition_id[market["condition_id"]] = market
        price_history = history_results.get(market["condition_id"], [])
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
