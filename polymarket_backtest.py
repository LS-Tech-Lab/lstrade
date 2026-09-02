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
from polymarket_categories import categorize
from polymarket_client import PolymarketClient
from polymarket_signal_engine import generate_polymarket_signal

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("polymarket_backtest")


def fetch_closed_markets(client, limit, min_volume=5000, max_offset=1900, max_chunks=30):
    """
    Trae mercados YA RESUELTOS (closed=true), filtrados por volumen TOTAL
    (no liquidez — ver limitaciones arriba).

    Gamma corta con 422 pasado cierto offset (~2000-2100 en la práctica,
    parece un techo duro de la API, no algo que dependa del filtro) — pedir
    `--limit` más alto no alcanza más mercados si nos quedamos en una sola
    ventana de paginación. Para superar eso, se trochea la consulta por
    fecha de resolución: se pide la página más reciente ordenada por
    `endDate`, se toma la fecha más vieja del lote como próximo cursor
    (`end_date_max`), y se repite — cada "trozo" de tiempo se pagina por
    separado, cada uno bien por debajo del techo de offset.

    `end_date_max` no está en la documentación oficial que pude confirmar
    en el momento de escribir esto (sí aparece documentado para el
    endpoint /events de un proxy tipado de la API, que normalmente refleja
    los mismos filtros que /markets) — así que esta función VALIDA que
    Gamma lo esté respetando de verdad: si el primer trozo con cursor trae
    fechas más nuevas que el cursor pedido, asume que el filtro no está
    soportado, devuelve lo que ya juntó de la primera ventana (el mismo
    comportamiento de antes) y no sigue troceando a ciegas.
    """
    markets = []
    seen_condition_ids = set()
    end_date_cursor = None
    date_filter_verified = None  # None = todavía no se probó

    for _ in range(max_chunks):
        if len(markets) >= limit:
            break

        offset = 0
        chunk_end_dates = []
        chunk_broke_filter = False
        while offset < max_offset:
            extra_params = {"order": "endDate", "ascending": "false"}
            if end_date_cursor:
                extra_params["end_date_max"] = end_date_cursor

            batch = client.fetch_active_markets(limit=100, offset=offset, closed=True, extra_params=extra_params)
            if not batch:
                break

            page_dates = [raw["endDate"] for raw in batch if raw.get("endDate")]

            # Validar apenas llega la PRIMERA página de un trozo con cursor —
            # si Gamma no respeta end_date_max, ya se nota acá (la página
            # más reciente ordenada por endDate va a traer fechas más
            # nuevas que el cursor pedido) y no hace falta pagar el costo
            # de paginar todo el trozo entero para descubrirlo.
            if end_date_cursor and offset == 0 and date_filter_verified is None and page_dates:
                date_filter_verified = max(page_dates) <= end_date_cursor
                if not date_filter_verified:
                    log.warning(
                        "Gamma no parece estar respetando end_date_max en /markets — el troceo por "
                        "fecha no está confirmado para este endpoint. Se sigue con lo que ya se juntó "
                        "de la ventana de paginación disponible (igual que antes de este intento)."
                    )
                    chunk_broke_filter = True
                    break

            for raw in batch:
                cid = raw.get("conditionId")
                if cid and cid in seen_condition_ids:
                    continue
                if cid:
                    seen_condition_ids.add(cid)
                parsed = client.parse_market_for_analysis(raw)
                if parsed and parsed["volume_total"] >= min_volume and parsed.get("yes_token_id"):
                    markets.append(parsed)

            chunk_end_dates.extend(page_dates)
            offset += 100
            if len(batch) < 100:
                break

        if chunk_broke_filter:
            break

        if not chunk_end_dates:
            break

        new_cursor = min(chunk_end_dates)
        if end_date_cursor is not None and new_cursor >= end_date_cursor:
            break  # el cursor no avanzó — evita loop sin sentido
        end_date_cursor = new_cursor

    return markets[:limit]


def backtest_market(config, market, yes_history, no_history, min_confidence=1):
    """
    Camina yes_history punto a punto (asume que no_history está alineado por
    índice — ambos vienen del mismo endpoint con el mismo `fidelity`).

    min_confidence (2026-09-02): mismo filtro que config.POLYMARKET_MIN_CONFIDENCE
    en producción (polymarket_main.py) — se aplica acá también para que el
    backtest mida la estrategia tal cual corre en vivo, no una versión sin
    el filtro de confianza.
    """
    trades = []
    warmup = 13
    in_trade = None
    diagnostics = {"no_signal": 0, "signal_no_trade_plan": 0, "below_min_confidence": 0, "excluded_category": 0}
    category = categorize(market["question"])
    excluded = category in getattr(config, "POLYMARKET_EXCLUDED_CATEGORIES", [])

    n = min(len(yes_history), len(no_history)) if no_history else len(yes_history)

    if excluded:
        diagnostics["excluded_category"] = n - warmup if n > warmup else 0
        return trades, diagnostics

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
                    "category": category,
                    "direction": in_trade["direction"],
                    "confidence": in_trade["confidence"],
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
            target_pct_min=config.POLYMARKET_TARGET_PCT_MIN,
            target_pct_max=config.POLYMARKET_TARGET_PCT_MAX,
        )
        if not signal:
            diagnostics["no_signal"] += 1
            continue
        if signal["confidence"] < min_confidence:
            diagnostics["below_min_confidence"] = diagnostics.get("below_min_confidence", 0) + 1
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
            "confidence": signal["confidence"],
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
    parser.add_argument("--min-confidence", type=int, default=None,
                         help="confianza mínima (1-5) a simular; por defecto usa config.POLYMARKET_MIN_CONFIDENCE")
    parser.add_argument("--output", help="CSV opcional con el detalle de cada trade simulado")
    args = parser.parse_args()

    config = Config
    client = PolymarketClient(config)
    min_confidence = args.min_confidence if args.min_confidence is not None else config.POLYMARKET_MIN_CONFIDENCE

    log.info(f"Buscando hasta {args.limit} mercados cerrados con volumen total >= {args.min_volume}...")
    log.info(f"Confianza mínima a simular: {min_confidence}/5 | Categorías excluidas: {config.POLYMARKET_EXCLUDED_CATEGORIES}")
    markets = fetch_closed_markets(client, args.limit, args.min_volume)
    log.info(f"{len(markets)} mercados encontrados. Descargando historial de precios...")

    all_trades = []
    skipped_short_history = 0
    total_diag = {"no_signal": 0, "signal_no_trade_plan": 0, "below_min_confidence": 0, "excluded_category": 0}
    for i, market in enumerate(markets, 1):
        yes_history = client.fetch_price_history(market["yes_token_id"], interval="max", fidelity=60)
        no_history = client.fetch_price_history(market["no_token_id"], interval="max", fidelity=60) \
            if market.get("no_token_id") else []
        time.sleep(0.2)

        if len(yes_history) < 13:
            skipped_short_history += 1
            continue

        trades, diagnostics = backtest_market(config, market, yes_history, no_history, min_confidence=min_confidence)
        all_trades.extend(trades)
        for k in total_diag:
            total_diag[k] += diagnostics.get(k, 0)
        if trades:
            log.info(f"[{i}/{len(markets)}] {market['question'][:50]}: {len(trades)} trade(s) simulado(s)")

    log.info("=== RESUMEN GENERAL — Polymarket ===")
    stats = summarize(all_trades)
    if stats["n"] == 0:
        log.info(
            f"Sin trades simulados. Diagnóstico: {skipped_short_history} mercado(s) con menos de "
            f"13 puntos de historial (se saltearon enteros), {total_diag['no_signal']} evaluaciones "
            f"sin señal (ineficiencia/momentum por debajo del umbral), "
            f"{total_diag['excluded_category']} evaluaciones en mercados de categoría excluida, "
            f"{total_diag['below_min_confidence']} señales por debajo de confianza {min_confidence}/5, "
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
        by_conf = {}
        for t in all_trades:
            by_conf.setdefault(t["confidence"], []).append(t)
        for conf in sorted(by_conf, reverse=True):
            s = summarize(by_conf[conf])
            log.info(f"  confianza {conf}/5: {s['n']} trades | win rate {s['win_rate']:.1f}% | total {s['total_r']:.2f}R")

    if args.output and all_trades:
        with open(args.output, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(all_trades[0].keys()))
            writer.writeheader()
            writer.writerows(all_trades)
        log.info(f"Detalle de {len(all_trades)} trades guardado en {args.output}")


if __name__ == "__main__":
    main()
