"""
Backtest de signal_engine.generate_signal() sobre histórico real del exchange.

Corre símbolo por símbolo, de forma independiente entre sí — no simula
exposición cruzada entre símbolos ni el circuit breaker de drawdown del
sistema completo (eso es un problema aparte: simular todo el ciclo
multi-símbolo). Lo que sí responde es la pregunta previa a todo lo demás:
¿la lógica de generate_signal(), tal como está hoy, tiene una edge real
contra datos históricos, o los pesos del score fueron ajustados a ciegas?

Simplificaciones a tener en cuenta al leer los resultados:
- Por defecto, cada trade sale exactamente en el stop o el target fijo
  (ATR_STOP_MULT / MIN_RR), como si no existiera gestión activa de la
  posición. Con --simulate-trailing se replica la misma lógica de
  position_manager.py (breakeven a 1 ATR, trailing a 1.5 ATR) vela a vela,
  para que el win rate/expectancy reportado se parezca al del sistema real
  en producción — sin esto, el número que da el backtest NO es el de tu
  sistema real, es el de una versión sin gestión activa.
- Sin comisiones ni slippage — es el escenario optimista.
- Si una misma vela toca stop y target a la vez, se asume el peor caso
  (stop) para no inflar el resultado.
- Con --oos-frac se separa el período en in-sample/out-of-sample por fecha
  (los primeros (1-frac) para ajustar, el resto para validar) y se reportan
  las dos partes por separado — corre la lógica actual tal cual (no reajusta
  nada), pero deja ver si la edge se sostiene fuera de la ventana que
  probablemente se usó para calibrar los pesos del score a mano.

Uso:
    python backtest.py --days 180
    python backtest.py --symbol ETH/USDT --days 90
    python backtest.py --days 365 --output resultados.csv
    python backtest.py --days 365 --simulate-trailing
    python backtest.py --days 365 --oos-frac 0.3
"""
import argparse
import csv
import logging
import time
from datetime import datetime, timedelta, timezone

import indicators as ind
from config import Config
from exchange_client import ExchangeClient
from risk_manager import adaptive_atr_stop_mult
from signal_engine import generate_signal

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("backtest")


def fetch_full_history(exchange_client, symbol, timeframe, since_ms, page_limit=1000):
    """Trae todo el histórico entre since_ms y ahora, paginando."""
    all_candles = []
    cursor = since_ms
    while True:
        batch = exchange_client.fetch_ohlcv(symbol, timeframe=timeframe, limit=page_limit, since=cursor)
        if not batch:
            break
        all_candles.extend(batch)
        last_ts = batch[-1]["ts"]
        if last_ts <= cursor or len(batch) < page_limit:
            break
        cursor = last_ts + 1
        time.sleep(getattr(exchange_client.exchange, "rateLimit", 200) / 1000)

    seen = set()
    deduped = []
    for c in all_candles:
        if c["ts"] not in seen:
            seen.add(c["ts"])
            deduped.append(c)
    return sorted(deduped, key=lambda c: c["ts"])


def slice_window_at(candles, ts, max_len):
    """Hasta max_len velas cuyo timestamp sea <= ts, sin mirar al futuro."""
    idx = 0
    for i, c in enumerate(candles):
        if c["ts"] > ts:
            break
        idx = i + 1
    return candles[max(0, idx - max_len):idx]


def compute_btc_bias(btc_4h_window):
    if not btc_4h_window or len(btc_4h_window) < 6:
        return None
    momentum = (btc_4h_window[-1]["c"] - btc_4h_window[-6]["c"]) / btc_4h_window[-6]["c"]
    if momentum > 0.015:
        return {"direction": "LONG"}
    elif momentum < -0.015:
        return {"direction": "SHORT"}
    return {"direction": "NEUTRAL"}


def _trailing_new_stop(direction, entry, atr_val, current_price, current_stop):
    """
    Misma lógica exacta que PositionManager.manage_open_positions():
    breakeven a 1 ATR de recorrido, luego trailing a 1.5 ATR del precio.
    Devuelve el stop actualizado (o el mismo si no corresponde moverlo).
    """
    if not atr_val or atr_val <= 0:
        return current_stop
    if direction == "LONG":
        if current_price > entry + atr_val and current_stop < entry:
            return entry
        if current_price > entry + (atr_val * 1.5):
            trail_stop = current_price - (atr_val * 1.5)
            if trail_stop > current_stop:
                return trail_stop
    else:
        if current_price < entry - atr_val and current_stop > entry:
            return entry
        if current_price < entry - (atr_val * 1.5):
            trail_stop = current_price + (atr_val * 1.5)
            if trail_stop < current_stop:
                return trail_stop
    return current_stop


def backtest_symbol(config, symbol, candles_1h, candles_4h, btc_4h, simulate_trailing=False):
    trades = []
    in_position = None
    warmup = max(60, config.CANDLE_LIMIT)

    for i in range(warmup, len(candles_1h)):
        current = candles_1h[i]

        if in_position:
            stop = in_position["stop"]

            # Trailing: actualizar el stop con lo que se sabía hasta el cierre
            # de la vela ANTERIOR (nunca con el high/low de la vela que se está
            # evaluando ahora mismo, para no meter lookahead).
            if simulate_trailing:
                atr_window = candles_1h[max(0, i - 20):i]
                atr_val = ind.atr(atr_window, 14)
                stop = _trailing_new_stop(
                    in_position["direction"], in_position["entry"], atr_val,
                    candles_1h[i - 1]["c"], stop,
                )
                in_position["stop"] = stop

            hit_stop = (current["l"] <= stop) if in_position["direction"] == "LONG" \
                else (current["h"] >= stop)
            hit_target = (current["h"] >= in_position["target"]) if in_position["direction"] == "LONG" \
                else (current["l"] <= in_position["target"])

            outcome, exit_price = None, None
            if hit_stop:
                outcome, exit_price = "stop", stop
            elif hit_target:
                outcome, exit_price = "target", in_position["target"]

            if outcome:
                sign = 1 if in_position["direction"] == "LONG" else -1
                r_multiple = ((exit_price - in_position["entry"]) / in_position["stop_distance"]) * sign
                trades.append({
                    "symbol": symbol,
                    "entry_ts": in_position["entry_ts"],
                    "exit_ts": current["ts"],
                    "direction": in_position["direction"],
                    "type": in_position["type"],
                    "outcome": outcome,
                    "r_multiple": r_multiple,
                    "momentum": in_position["momentum"],
                    "trend_align": in_position["trend_align"],
                    "volatility": in_position["volatility"],
                    "rsi": in_position["rsi"],
                    "volume_ratio": in_position["volume_ratio"],
                    "score": in_position["score"],
                })
                in_position = None
            continue

        window_1h = candles_1h[max(0, i - config.CANDLE_LIMIT + 1):i + 1]
        window_4h = slice_window_at(candles_4h, current["ts"], 50)
        btc_window = slice_window_at(btc_4h, current["ts"], 50)
        btc_bias = None if symbol == "BTC/USDT" else compute_btc_bias(btc_window)

        signal = generate_signal(window_1h, higher_tf_candles=window_4h, btc_bias=btc_bias)
        if not signal:
            continue

        atr_val = signal["atr"]
        stop_mult = adaptive_atr_stop_mult(config, signal["volatility"] * 100)
        stop_distance = atr_val * stop_mult
        if stop_distance <= 0:
            continue
        entry = signal["price"]
        direction = signal["direction"]
        if direction == "LONG":
            stop = entry - stop_distance
            target = entry + stop_distance * config.MIN_RR
        else:
            stop = entry + stop_distance
            target = entry - stop_distance * config.MIN_RR

        in_position = {
            "direction": direction, "stop": stop, "target": target,
            "entry": entry, "stop_distance": stop_distance,
            "entry_ts": current["ts"], "type": signal["type"],
            # Features de la señal en el momento de entrada — se guardan en el
            # trade para poder calibrar los pesos del score después con
            # calibrate_weights.py (regresión sobre resultado real vs features).
            "momentum": signal["momentum"], "trend_align": signal["trend_align"],
            "volatility": signal["volatility"], "rsi": signal["rsi"],
            "volume_ratio": signal["volume_ratio"], "score": signal["score"],
        }

    return trades


def summarize(trades):
    if not trades:
        return {"n": 0}
    wins = [t for t in trades if t["outcome"] == "target"]
    losses = [t for t in trades if t["outcome"] == "stop"]
    win_rate = len(wins) / len(trades) * 100
    avg_r = sum(t["r_multiple"] for t in trades) / len(trades)

    curve, peak, max_dd = 0.0, 0.0, 0.0
    for t in trades:
        curve += t["r_multiple"]
        peak = max(peak, curve)
        max_dd = max(max_dd, peak - curve)

    gross_win = sum(t["r_multiple"] for t in wins)
    gross_loss = abs(sum(t["r_multiple"] for t in losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else float("inf")

    return {
        "n": len(trades), "wins": len(wins), "losses": len(losses),
        "win_rate": win_rate, "expectancy_r": avg_r,
        "max_drawdown_r": max_dd, "profit_factor": profit_factor,
        "total_r": curve,
    }


def log_stats(label, stats):
    if stats["n"] == 0:
        log.info(f"{label}: sin señales en el período.")
        return
    log.info(
        f"{label}: {stats['n']} trades | win rate {stats['win_rate']:.1f}% | "
        f"expectancy {stats['expectancy_r']:.2f}R | drawdown máx {stats['max_drawdown_r']:.2f}R | "
        f"profit factor {stats['profit_factor']:.2f} | total {stats['total_r']:.2f}R"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", help="un solo símbolo (default: todos los de SYMBOLS en .env)")
    parser.add_argument("--days", type=int, default=180, help="días de histórico a descargar")
    parser.add_argument("--output", help="ruta de un CSV opcional con el detalle de cada trade")
    parser.add_argument("--simulate-trailing", action="store_true",
                         help="simula el trailing stop de position_manager.py en vez de stop/target fijos")
    parser.add_argument("--oos-frac", type=float, default=0.0,
                         help="fracción final del período (0-1) a reportar aparte como out-of-sample, "
                              "ej. 0.3 = últimos 30%% del rango. 0 (default) = sin split.")
    args = parser.parse_args()

    config = Config
    exchange_client = ExchangeClient(config)
    symbols = [args.symbol] if args.symbol else config.SYMBOLS
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    since_ms = int((datetime.now(timezone.utc) - timedelta(days=args.days)).timestamp() * 1000)
    oos_cutoff_ms = None
    if 0 < args.oos_frac < 1:
        oos_cutoff_ms = int(now_ms - (now_ms - since_ms) * args.oos_frac)
        log.info(f"Split walk-forward: in-sample hasta {datetime.fromtimestamp(oos_cutoff_ms/1000, tz=timezone.utc)}, "
                 f"out-of-sample después de eso.")

    log.info("Descargando histórico BTC/USDT 4h para el sesgo de mercado...")
    btc_4h = fetch_full_history(exchange_client, "BTC/USDT", "4h", since_ms)

    all_trades = []
    for symbol in symbols:
        log.info(f"--- {symbol} ---")
        candles_1h = fetch_full_history(exchange_client, symbol, config.TIMEFRAME, since_ms)
        candles_4h = fetch_full_history(exchange_client, symbol, "4h", since_ms)
        log.info(f"{symbol}: {len(candles_1h)} velas de {config.TIMEFRAME}, {len(candles_4h)} de 4h")

        trades = backtest_symbol(config, symbol, candles_1h, candles_4h, btc_4h,
                                  simulate_trailing=args.simulate_trailing)
        all_trades.extend(trades)
        log_stats(symbol, summarize(trades))

    log.info("=== RESUMEN GENERAL ===")
    log_stats("Todos los símbolos" + (" (trailing simulado)" if args.simulate_trailing else ""), summarize(all_trades))

    if oos_cutoff_ms is not None:
        in_sample = [t for t in all_trades if t["entry_ts"] < oos_cutoff_ms]
        out_sample = [t for t in all_trades if t["entry_ts"] >= oos_cutoff_ms]
        log.info("=== IN-SAMPLE (ventana de ajuste) ===")
        log_stats("In-sample", summarize(in_sample))
        log.info("=== OUT-OF-SAMPLE (validación) ===")
        log_stats("Out-of-sample", summarize(out_sample))
        log.info("Si el win rate/expectancy caen fuerte de in-sample a out-of-sample, "
                 "los pesos del score probablemente están sobreajustados a este período.")

    if args.output and all_trades:
        with open(args.output, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(all_trades[0].keys()))
            writer.writeheader()
            writer.writerows(all_trades)
        log.info(f"Detalle de {len(all_trades)} trades guardado en {args.output}")


if __name__ == "__main__":
    main()
