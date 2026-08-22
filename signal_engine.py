"""
Motor de señales. Traduce indicadores reales en un setup clasificado
(ruptura, pullback, momentum, continuación, reversión) — Python puro,
sin dependencias pesadas, para que corra igual en un VPS o en una
función serverless con cold-start ajustado.
"""
import indicators as ind


def generate_signal(candles, min_score=0.03):
    """
    candles: lista de dicts [{"o","h","l","c"}, ...] en orden cronológico, mínimo 30.
    Devuelve None si no hay señal con score suficiente, o un dict con la señal.
    """
    if len(candles) < 30:
        return None

    closes = [c["c"] for c in candles]
    sma_short = ind.sma(candles, 10)
    sma_long = ind.sma(candles, 30)
    if sma_short is None or sma_long is None:
        return None

    momentum = (closes[-1] - closes[-6]) / closes[-6]
    trend_align = (sma_short - sma_long) / sma_long
    vol = ind.rolling_return_stdev(candles, 10)
    rsi_val = ind.rsi(candles, 14)
    atr_val = ind.atr(candles, 14)

    if vol is None or atr_val is None:
        return None

    score = abs(momentum) * 1.2 + abs(trend_align) * 0.8 - vol * 1.5
    if score < min_score:
        return None

    last = closes[-1]
    rolling_max = max(c["h"] for c in candles[-20:])
    rolling_min = min(c["l"] for c in candles[-20:])
    direction = "LONG" if momentum >= 0 else "SHORT"

    if last >= rolling_max * 0.998 and momentum > 0:
        setup_type = "RUPTURA"
    elif momentum > 0 and last < rolling_max * 0.99 and last > rolling_min * 1.01:
        setup_type = "PULLBACK"
    elif abs(trend_align) > 0.004:
        setup_type = "CONTINUACION"
    elif (momentum > 0) != (trend_align > 0) and abs(trend_align) > 0.0008:
        setup_type = "REVERSION"
    else:
        setup_type = "MOMENTUM"

    raw_conf = min(1.0, max(0.0, abs(momentum) * 30 + abs(trend_align) * 50 - vol * 6 + 0.3))
    confidence = max(1, min(5, round(raw_conf * 5)))

    return {
        "type": setup_type,
        "direction": direction,
        "confidence": confidence,
        "momentum": float(momentum),
        "trend_align": float(trend_align),
        "volatility": float(vol),
        "rsi": rsi_val,
        "atr": float(atr_val),
        "price": float(last),
        "score": float(score),
    }
