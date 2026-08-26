"""
Motor de señales MEJORADO con filtros de Volumen, RSI y MTF.
"""
import indicators as ind

def generate_signal(candles, higher_tf_candles=None, btc_bias=None, min_score=0.03):
    if len(candles) < 30:
        return None
    
    closes = [c["c"] for c in candles]
    
    sma_short = ind.sma(candles, 10)
    sma_long = ind.sma(candles, 30)
    ema_mid = ind.ema(candles, 20)
    ema_trend = ind.ema(candles, 50)
    
    if sma_short is None or sma_long is None or ema_mid is None or ema_trend is None:
        return None
    
    momentum = (closes[-1] - closes[-6]) / closes[-6]
    trend_align = (sma_short - sma_long) / sma_long
    vol = ind.rolling_return_stdev(candles, 10)
    rsi_val = ind.rsi(candles, 14)
    atr_val = ind.atr(candles, 14)
    vol_ratio = ind.volume_ratio(candles, 20)
    
    if vol is None or atr_val is None or rsi_val is None or vol_ratio is None:
        return None
    
    # FILTRO 1: Volumen (evita trampas sin fuerza)
    if vol_ratio < 0.8:
        return None
    
    # FILTRO 2: RSI (evita sobrecompra/sobreventa extrema)
    direction = "LONG" if momentum >= 0 else "SHORT"
    if direction == "LONG" and rsi_val > 72:
        return None
    if direction == "SHORT" and rsi_val < 28:
        return None
    
    # FILTRO 3: Análisis Multi-Timeframe (MTF)
    if higher_tf_candles and len(higher_tf_candles) >= 50:
        ht_ema_50 = ind.ema(higher_tf_candles, 50)
        ht_last_close = higher_tf_candles[-1]["c"]
        if direction == "LONG" and ht_last_close < ht_ema_50:
            return None  # No comprar si la tendencia de 4H es bajista
        if direction == "SHORT" and ht_last_close > ht_ema_50:
            return None  # No vender si la tendencia de 4H es alcista

    # FILTRO 4: Tendencia de BTC (para altcoins)
    if btc_bias and btc_bias.get("direction") in ("LONG", "SHORT"):
        btc_dir = btc_bias["direction"]
        if direction == "LONG" and btc_dir == "SHORT":
            return None
        if direction == "SHORT" and btc_dir == "LONG":
            return None
    
    # Cálculo del Score
    score = abs(momentum) * 1.2 + abs(trend_align) * 0.8 - vol * 1.5
    if vol_ratio > 1.5:
        score += 0.02
    if vol_ratio > 2.0:
        score += 0.02
    
    if score < min_score:
        return None
    
    last = closes[-1]
    rolling_max = max(c["h"] for c in candles[-20:])
    rolling_min = min(c["l"] for c in candles[-20:])
    
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
        "volume_ratio": float(vol_ratio),
        "price": float(last),
        "score": float(score),
    }