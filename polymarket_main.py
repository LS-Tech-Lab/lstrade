"""
Orquestador principal para Polymarket — módulo independiente del bot de cripto.
Ahora envía tanto las señales como el Market Watch a Telegram.
"""
import argparse
import logging
import sys
import time
from config import Config
from polymarket_client import PolymarketClient
from polymarket_signal_engine import generate_polymarket_signal
from polymarket_state import PolymarketStateStore
from telegram_notifier import TelegramNotifier

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("polymarket_main")


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
    lines.append("")
    lines.append("_⚠️ MODO LECTURA — No se ejecutó ninguna operación real._")
    
    return "\n".join(lines)


def run_polymarket_cycle(config, client, notifier, state_store, top_n=None):
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
    
    for market_raw in markets_raw:
        market = client.parse_market_for_analysis(market_raw)
        if not market:
            continue
        
        parsed_markets.append(market)
        
        if market["liquidity"] < 1000:
            continue

        if not market.get("yes_token_id"):
            log.warning(f"Sin clobTokenId resuelto para '{market['question'][:50]}', se omite el historial de precios.")
            price_history = []
        else:
            price_history = client.fetch_price_history(market["yes_token_id"], interval="1h", fidelity=60)
        time.sleep(0.2)
        
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
        time.sleep(0.5)
    
    return signals


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
        run_polymarket_cycle(config, client, notifier, state_store, top_n=args.top)
        return
    
    while True:
        try:
            run_polymarket_cycle(config, client, notifier, state_store, top_n=args.top)
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