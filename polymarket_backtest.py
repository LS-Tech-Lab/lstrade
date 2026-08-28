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
- Los mercados cerrados/resueltos tienen `liquidity` estructuralmente en 0
  (el order book se cierra al resolver) — el componente de "rotación de
  capital" del score (`volume_24h/liquidity`) nunca se activa en este
  backtest, aunque sí se activa en producción con mercados abiertos. La
  ineficiencia de precio y el momentum (los dos componentes que determinan
  si hay señal y con qué dirección) sí son 100% reales e históricos, así
  que el backtest sigue siendo representativo de la parte que más importa
  — solo puede estar subestimando levemente el score de señales que en
  vivo también tendrían bonus por liquidez.
- Filtramos mercados por volumen TOTAL (no por liquidez, que sería ~0 en
  casi todos los mercados cerrados).
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


def fetch_closed_markets(client, limit, min_volume=5000, max_offset=5000):
    """
    Trae mercados YA RESUELTOS (closed=true), filtrados por volumen TOTAL
    (no liquidez — ver limitaciones arriba). `max_offset` frena la
    paginación antes de pegarle a un límite de la API de Gamma (algunas
    consultas devuelven 422 en offsets muy altos si se pide más de lo que
    hay disponible).
    """
    markets = []
    offset = 0
    page = 100
    while len(markets) < limit and offset < max_offset:
        batch = client.fetch_active_markets(limit=page, offset=offset, closed=True)
        if not batch:
            break
        for raw in batch:
            parsed = client.parse_market_for_analysis(raw)
            if parsed and parsed["volume_total"] >= min_volume and parsed.get("yes_token_id"):
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
    diagnostics = {"no_signal": 0, "signal_no_trade_plan": 0}

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

        # NOTA (bug corregido): analyze_probability_momentum() usa por defecto
        # window=12 y exige len(price_history) >= window+1 = 13 puntos, pero
        # acá se le pasaba una ventana de 12 — un punto corto. Con eso,
        # momentum_data quedaba SIEMPRE en None dentro de
        # generate_polymarket_signal(), y como la dirección de la señal
        # depende obligatoriamente de haber momentum real (línea que evita el
        # fallback sin fundamento), nunca se generaba ninguna señal en todo
        # el backtest — no era un problema de datos ni de umbrales.
        price_window = yes_history[max(0, i - 12):i + 1]

        signal = generate_polymarket_signal(
            snapshot, price_window,
            min_score=0.03,
            stop_vol_mult=config.POLYMARKET_STOP_VOL_MULT,
            target_rr=config.POLYMARKET_TARGET_RR,
        )
        if not signal:
            diagnostics["no_signal"] += 1
            continue
        if not signal.get("trade_plan"):
            diagnostics["signal_no_trade_plan"] += 1
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

    return trades, diagnostics


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
    parser.add_argument("--min-volume", type=float, default=5000, help="volumen total mínimo para considerar el mercado")
    parser.add_argument("--output", help="CSV opcional con el detalle de cada trade simulado")
    args = parser.parse_args()

    config = Config
    client = PolymarketClient(config)

    log.info(f"Buscando hasta {args.limit} mercados cerrados con volumen total >= {args.min_volume}...")
    markets = fetch_closed_markets(client, args.limit, args.min_volume)
    log.info(f"{len(markets)} mercados encontrados. Descargando historial de precios...")

    all_trades = []
    skipped_short_history = 0
    total_diag = {"no_signal": 0, "signal_no_trade_plan": 0}
    for i, market in enumerate(markets, 1):
        yes_history = client.fetch_price_history(market["yes_token_id"], interval="max", fidelity=60)
        no_history = client.fetch_price_history(market["no_token_id"], interval="max", fidelity=60) \
            if market.get("no_token_id") else []
        time.sleep(0.2)

        if len(yes_history) < 13:
            skipped_short_history += 1
            continue

        trades, diagnostics = backtest_market(config, market, yes_history, no_history)
        all_trades.extend(trades)
        for k in total_diag:
            total_diag[k] += diagnostics[k]
        if trades:
            log.info(f"[{i}/{len(markets)}] {market['question'][:50]}: {len(trades)} trade(s) simulado(s)")

    log.info("=== RESUMEN GENERAL — Polymarket ===")
    stats = summarize(all_trades)
    if stats["n"] == 0:
        log.info(
            f"Sin trades simulados. Diagnóstico: {skipped_short_history} mercado(s) con menos de "
            f"13 puntos de historial (se saltearon enteros), {total_diag['no_signal']} evaluaciones "
            f"sin señal (ineficiencia/momentum por debajo del umbral), "
            f"{total_diag['signal_no_trade_plan']} señales sin plan de salida (volatilidad nula). "
            f"Si la mayoría cae en 'sin señal', probá con --limit más alto o revisá "
            f"min_score/umbrales en polymarket_signal_engine.py."
        )
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
