"""
Motor de señales MEJORADO. Traduce indicadores reales en un setup clasificado
con filtros adicionales de volumen, RSI y tendencia de BTC.
"""
import indicators as ind

def generate_signal(candles, min_score=0.03, btc_bias=None):
    """
    candles: lista de dicts [{"o","h","l","c","v"}, ...] en orden cronológico, mínimo 30.
    btc_bias: dict opcional con {"direction": "LONG"/"SHORT"/"NEUTRAL"} 
              para filtrar altcoins contra la tendencia de BTC.
    Devuelve None si no hay señal con score suficiente o si falla algún filtro.
    """
    if len(candles) < 30:
        return None
    
    closes = [c["c"] for c in candles]
    
    # --- Indicadores base ---
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
    vol_ratio = ind.volume_ratio(candles, 20)  # NUEVO: filtro de volumen
    
    if vol is None or atr_val is None or rsi_val is None or vol_ratio is None:
        return None
    
    # --- FILTRO 1: Volumen (evita trampas sin fuerza) ---
    if vol_ratio < 0.8:
        return None  # Volumen muy bajo, no hay interés real
    
    # --- FILTRO 2: RSI (evita sobrecompra/sobreventa extrema) ---
    last = closes[-1]
    direction = "LONG" if momentum >= 0 else "SHORT"
    
    if direction == "LONG" and rsi_val > 72:
        return None  # Sobrecomprado, alto riesgo de corrección
    if direction == "SHORT" and rsi_val < 28:
        return None  # Sobreventa, alto riesgo de rebote
    
    # --- FILTRO 3: Alineación EMA (doble confirmación de tendencia) ---
    ema_aligned_long = (ema_mid > ema_trend) and (last > ema_mid)
    ema_aligned_short = (ema_mid < ema_trend) and (last < ema_mid)
    
    if direction == "LONG" and not ema_aligned_long:
        # Penaliza pero no bloquea si el momentum es muy fuerte
        if momentum < 0.02:
            return None
    if direction == "SHORT" and not ema_aligned_short:
        if momentum > -0.02:
            return None
    
    # --- FILTRO 4: Tendencia de BTC (solo para altcoins) ---
    if btc_bias and btc_bias.get("direction") in ("LONG", "SHORT"):
        btc_dir = btc_bias["direction"]
        # Si operamos altcoins en contra de BTC, bloquear
        if direction == "LONG" and btc_dir == "SHORT":
            return None  # No comprar altcoins si BTC está cayendo
        if direction == "SHORT" and btc_dir == "LONG":
            return None  # No vender altcoins si BTC está subiendo
    
    # --- Score de la señal ---
    score = abs(momentum) * 1.2 + abs(trend_align) * 0.8 - vol * 1.5
    # Bonus por volumen fuerte
    if vol_ratio > 1.5:
        score += 0.02
    if vol_ratio > 2.0:
        score += 0.02
    
    if score < min_score:
        return None
    
    # --- Clasificación del setup ---
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
    
    # --- Confianza ---
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
        "volume_ratio": float(vol_ratio),  # NUEVO
        "price": float(last),
        "score": float(score),
    }