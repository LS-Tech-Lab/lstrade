"""
Orquestador principal para Polymarket — módulo independiente del bot de cripto.
"""
import argparse
import logging
import sys
import time
from config import Config
from polymarket_client import PolymarketClient
from polymarket_signal_engine import generate_polymarket_signal

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("polymarket_main")

def build_polymarket_memo(signal, markdown=False):
    m = signal["market"]
    lines = []
    title = f"🎯 SEÑAL POLYMARKET — {m['question'][:80]}"
    lines.append(f"**{title}**" if markdown else title)
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
    
    lines.append("")
    lines.append("🔍 Razones:")
    for r in signal.get("reasons", []):
        lines.append(f"  • {r}")
    
    return "\n".join(lines)

def run_polymarket_cycle(config, client, top_n=None):
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
        
        # Filtrar mercados con liquidez muy baja
        if market["liquidity"] < 1000:
            continue
        
        # Obtener historial de precios (respetando rate limits)
        price_history = client.fetch_price_history(market["condition_id"], interval="1h", fidelity=60)
        time.sleep(0.2)
        
        signal = generate_polymarket_signal(market, price_history)
        if signal:
            signals.append(signal)
    
    # Mostrar Market Watch (Top 5 por volumen, haya señal o no)
    print("\n" + "=" * 70)
    print("👁️  MARKET WATCH — Top 5 Mercados por Volumen (Tiempo Real)")
    print("=" * 70)
    sorted_markets = sorted(parsed_markets, key=lambda x: x["volume_24h"], reverse=True)[:5]
    for i, m in enumerate(sorted_markets, 1):
        print(f"{i}. {m['question'][:60]}...")
        print(f"   💰 YES: ${m['yes_price']:.3f} | NO: ${m['no_price']:.3f} | 💧 Liq: ${m['liquidity']:,.0f}")
    print("=" * 70 + "\n")
    
    signals.sort(key=lambda s: s["score"], reverse=True)
    
    if not signals:
        log.info("Sin señales de alta probabilidad en Polymarket este ciclo (los filtros de momentum/ineficiencia no se activaron).")
        return []
    
    log.info(f"🎯 {len(signals)} señales detectadas en Polymarket:")
    for i, signal in enumerate(signals[:3], 1):
        print("\n" + "=" * 70)
        print(f"SEÑAL #{i}")
        print(build_polymarket_memo(signal))
        print("=" * 70)
    
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
    
    client = PolymarketClient(config)
    
    if args.once:
        run_polymarket_cycle(config, client, top_n=args.top)
        return
    
    while True:
        try:
            run_polymarket_cycle(config, client, top_n=args.top)
        except KeyboardInterrupt:
            log.info("Detenido manualmente por el usuario.")
            break
        except Exception as e:
            log.exception(f"Error inesperado en el ciclo: {e}")
        
        log.info(f"Próximo ciclo en {args.loop_interval} segundos...")
        time.sleep(args.loop_interval)

if __name__ == "__main__":
    main()