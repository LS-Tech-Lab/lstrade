"""
Backtest de generate_polymarket_signal() sobre mercados de Polymarket YA
RESUELTOS — hasta ahora este módulo no tenía ninguna forma de saber si tiene
una edge real; esto es el equivalente de backtest.py pero para Polymarket.

Cómo funciona:
- Trae mercados cerrados (resueltos) vía Gamma, ordenados por volumen.
- Para cada uno, trae el historial de precios de los tokens YES y NO
  (mismo endpoint CLOB que usa polymarket_client.py en producción).
- Camina el historial punto a punto: en cada paso arma un snapshot del
  mercado "como se veía en ese momento" (yes_price/no_price de esa vela,
  más liquidez/volumen actuales del mercado como aproximación — ver
  limitaciones abajo) y llama a generate_polymarket_signal() exactamente
  igual que en producción. Si hay señal con plan de salida, simula hacia
  adelante hasta que el precio de esa punta (YES o NO) toque el target o
  el stop.

Limitaciones a tener en cuenta al leer los resultados (igual de honestas
que las que ya documenta backtest.py para cripto):
- Liquidez y volumen 24h son los ACTUALES del mercado (al momento de correr
  el backtest), no los históricos de cada punto en el tiempo — Gamma no
  expone esa serie histórica fácilmente. El componente de "rotación de
  capital" del score puede estar usando datos algo desalineados en el
  tiempo. La ineficiencia de precio y el momentum sí son 100% históricos.
- Si un mercado nunca tuvo momentum > 2% en todo su historial, nunca genera
  señal (mismo comportamiento que producción — no hay fallback sin
  fundamento, a propósito).
- Sin comisiones ni slippage de libro de órdenes real.
- No usa el resultado final de resolución del mercado (YES=1/NO=0) para
  nada — el trade se cierra en target/stop antes de resolución, como
  describe el plan de salida sugerido del README. Es intencional: el
  objetivo es medir la estrategia de "revender antes", no "esperar a que
  resuelva".

Uso:
    python polymarket_backtest.py --limit 100
    python polymarket_backtest.py --limit 200 --output resultados_pm.csv
"""
import argparse
import csv
import logging
import time

from config import Config
from polymarket_client import PolymarketClient
from polymarket_signal_engine import generate_polymarket_signal

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("polymarket_backtest")


def fetch_closed_markets(client, limit, min_liquidity=1000):
    markets = []
    offset = 0
    page = 100
    while len(markets) < limit:
        batch = client.fetch_active_markets(limit=page, offset=offset, closed=True)
        if not batch:
            break
        for raw in batch:
            parsed = client.parse_market_for_analysis(raw)
            if parsed and parsed["liquidity"] >= min_liquidity and parsed.get("yes_token_id"):
                markets.append(parsed)
        offset += page
        if len(batch) < page:
            break
    return markets[:limit]


def backtest_market(config, market, yes_history, no_history):
    """
    Camina yes_history punto a punto (asume que no_history está alineado por
    índice — ambos vienen del mismo endpoint con el mismo `fidelity`).
    """
    trades = []
    warmup = 13
    in_trade = None

    n = min(len(yes_history), len(no_history)) if no_history else len(yes_history)

    for i in range(warmup, n):
        if in_trade:
            side_history = yes_history if in_trade["direction"] == "YES" else no_history
            current_price = side_history[i]["p"]
            hit_target = current_price >= in_trade["target"]
            hit_stop = current_price <= in_trade["stop"]
            if hit_target or hit_stop:
                outcome = "target" if hit_target else "stop"
                exit_price = in_trade["target"] if hit_target else in_trade["stop"]
                sign = 1  # el precio de la punta elegida siempre sube si gana, baja si pierde
                r_multiple = ((exit_price - in_trade["entry"]) / in_trade["stop_distance"]) * sign
                trades.append({
                    "question": market["question"][:60],
                    "direction": in_trade["direction"],
                    "outcome": outcome,
                    "r_multiple": r_multiple,
                    "entry_idx": in_trade["entry_idx"],
                    "exit_idx": i,
                })
                in_trade = None
            continue

        yes_price = yes_history[i]["p"]
        no_price = no_history[i]["p"] if no_history else max(0.01, 1 - yes_price)

        snapshot = dict(market)
        snapshot["yes_price"] = yes_price
        snapshot["no_price"] = no_price
        snapshot["closed"] = False  # se está evaluando "como si" el mercado siguiera abierto en ese punto

        price_window = yes_history[max(0, i - 11):i + 1]

        signal = generate_polymarket_signal(
            snapshot, price_window,
            min_score=0.03,
            stop_vol_mult=config.POLYMARKET_STOP_VOL_MULT,
            target_rr=config.POLYMARKET_TARGET_RR,
        )
        if not signal or not signal.get("trade_plan"):
            continue

        tp = signal["trade_plan"]
        stop_distance = abs(tp["entry"] - tp["stop"])
        if stop_distance <= 0:
            continue

        in_trade = {
            "direction": signal["direction"],
            "entry": tp["entry"], "target": tp["target"], "stop": tp["stop"],
            "stop_distance": stop_distance, "entry_idx": i,
        }

    return trades


def summarize(trades):
    if not trades:
        return {"n": 0}
    wins = [t for t in trades if t["outcome"] == "target"]
    win_rate = len(wins) / len(trades) * 100
    avg_r = sum(t["r_multiple"] for t in trades) / len(trades)
    gross_win = sum(t["r_multiple"] for t in trades if t["r_multiple"] > 0)
    gross_loss = abs(sum(t["r_multiple"] for t in trades if t["r_multiple"] < 0))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
    return {
        "n": len(trades), "win_rate": win_rate, "expectancy_r": avg_r,
        "profit_factor": profit_factor, "total_r": sum(t["r_multiple"] for t in trades),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=100, help="cuántos mercados cerrados analizar")
    parser.add_argument("--min-liquidity", type=float, default=1000, help="liquidez mínima para considerar el mercado")
    parser.add_argument("--output", help="CSV opcional con el detalle de cada trade simulado")
    args = parser.parse_args()

    config = Config
    client = PolymarketClient(config)

    log.info(f"Buscando hasta {args.limit} mercados cerrados con liquidez >= {args.min_liquidity}...")
    markets = fetch_closed_markets(client, args.limit, args.min_liquidity)
    log.info(f"{len(markets)} mercados encontrados. Descargando historial de precios...")

    all_trades = []
    for i, market in enumerate(markets, 1):
        yes_history = client.fetch_price_history(market["yes_token_id"], interval="max", fidelity=60)
        no_history = client.fetch_price_history(market["no_token_id"], interval="max", fidelity=60) \
            if market.get("no_token_id") else []
        time.sleep(0.2)

        if len(yes_history) < 13:
            continue

        trades = backtest_market(config, market, yes_history, no_history)
        all_trades.extend(trades)
        if trades:
            log.info(f"[{i}/{len(markets)}] {market['question'][:50]}: {len(trades)} trade(s) simulado(s)")

    log.info("=== RESUMEN GENERAL — Polymarket ===")
    stats = summarize(all_trades)
    if stats["n"] == 0:
        log.info("Sin trades simulados en el período — probá con --limit más alto o revisá "
                  "min_score/umbrales en polymarket_signal_engine.py.")
    else:
        log.info(
            f"{stats['n']} trades | win rate {stats['win_rate']:.1f}% | "
            f"expectancy {stats['expectancy_r']:.2f}R | profit factor {stats['profit_factor']:.2f} | "
            f"total {stats['total_r']:.2f}R"
        )

    if args.output and all_trades:
        with open(args.output, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(all_trades[0].keys()))
            writer.writeheader()
            writer.writerows(all_trades)
        log.info(f"Detalle de {len(all_trades)} trades guardado en {args.output}")


if __name__ == "__main__":
    main()
