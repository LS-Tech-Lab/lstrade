"""
Motor de señales para Polymarket MEJORADO.
Filtra mercados extremos (trampas de liquidez) y se enfoca en ineficiencias reales.
"""
import logging
from datetime import datetime

log = logging.getLogger("polymarket_signal_engine")

def analyze_probability_momentum(price_history, window=12):
    if len(price_history) < window + 1:
        return None

    prices = [p["p"] for p in price_history[-(window + 1):]]
    momentum = (prices[-1] - prices[0]) / prices[0] if prices[0] > 0 else 0
    avg_price = sum(prices[:-1]) / len(prices[:-1])
    trend = (prices[-1] - avg_price) / avg_price if avg_price > 0 else 0
    
    returns = []
    for i in range(1, len(prices)):
        if prices[i - 1] > 0:
            returns.append((prices[i] - prices[i - 1]) / prices[i - 1])
    
    if len(returns) < 2:
        volatility = 0
    else:
        mean_ret = sum(returns) / len(returns)
        variance = sum((r - mean_ret) ** 2 for r in returns) / len(returns)
        volatility = variance ** 0.5
    
    return {
        "momentum": momentum,
        "trend": trend,
        "volatility": volatility,
        "current_price": prices[-1],
        "min_price": min(prices),
        "max_price": max(prices),
    }

def detect_inefficiency(market):
    yes = market["yes_price"]
    no = market["no_price"]
    total = yes + no
    inefficiency = abs(total - 1.0)
    
    # NUEVO: Evitar trampas de liquidez en precios extremos
    is_extreme_trap = (yes < 0.05 or yes > 0.95 or no < 0.05 or no > 0.95)
    
    return {
        "inefficiency": inefficiency,
        "is_extreme_trap": is_extreme_trap,
        "total_implied_prob": total,
    }

def calculate_time_to_resolution(market):
    if not market.get("end_date"):
        return None
    try:
        end_date = datetime.fromisoformat(market["end_date"].replace("Z", "+00:00"))
        now = datetime.now(end_date.tzinfo)
        delta = end_date - now
        return delta.total_seconds() / 86400
    except Exception:
        return None

def generate_polymarket_signal(market, price_history=None, min_score=0.03,
                                stop_vol_mult=3.0, target_rr=1.5):
    if not market or market.get("closed"):
        return None
    
    if market["yes_price"] <= 0 or market["no_price"] <= 0:
        return None
    
    inefficiency_data = detect_inefficiency(market)
    
    # 🚨 FILTRO CRÍTICO: Rechazar mercados con probabilidad extrema (< 5% o > 95%)
    # No hay edge real ahí, solo riesgo de liquidez.
    if inefficiency_data["is_extreme_trap"]:
        return None

    momentum_data = None
    if price_history and len(price_history) >= 12:
        momentum_data = analyze_probability_momentum(price_history)
    
    days_to_resolution = calculate_time_to_resolution(market)
    
    score = 0.0
    reasons = []
    has_primary_signal = False

    # 1. Ineficiencia de precios (suma != 1.00) — factor primario
    if inefficiency_data["inefficiency"] > 0.02:
        score += inefficiency_data["inefficiency"] * 2
        reasons.append(f"Ineficiencia de precio: {inefficiency_data['total_implied_prob']:.3f}")
        has_primary_signal = True

    # 2. Momentum fuerte y real (cambio > 3% en el período) — factor primario
    if momentum_data:
        abs_momentum = abs(momentum_data["momentum"])
        if abs_momentum > 0.03:
            score += abs_momentum * 1.5
            reasons.append(f"Momentum fuerte: {momentum_data['momentum']*100:+.2f}%")
            has_primary_signal = True

        if momentum_data["volatility"] > 0.02:
            score += 0.01

    # Sin ineficiencia real NI momentum real no hay edge — no seguir sumando
    # factores secundarios que por sí solos no significan nada (evita señales
    # como "0.59x de rotación + resolución en 6.9 días" sin ningún fundamento).
    if not has_primary_signal:
        return None

    # 3. Volumen alto relativo a liquidez (interés real) — solo suma como bonus
    if market["liquidity"] > 0:
        vol_liq_ratio = market["volume_24h"] / market["liquidity"]
        if vol_liq_ratio > 0.5:
            score += 0.02
            reasons.append(f"Alta rotación de capital: {vol_liq_ratio:.2f}x")

    # 4. Resolución próxima (catalizador de volatilidad) — solo suma como bonus
    if days_to_resolution is not None:
        if 1 <= days_to_resolution <= 7:
            score += 0.02
            reasons.append(f"Resolución inminente: {days_to_resolution:.1f} días")

    if score < min_score:
        return None

    # Determinar dirección: solo si hay momentum real. Sin eso no hay
    # fundamento para elegir un lado — antes acá había un fallback que
    # apostaba en contra del precio más caro sin ninguna base real.
    if not (momentum_data and abs(momentum_data["momentum"]) > 0.02):
        return None
    direction = "YES" if momentum_data["momentum"] > 0 else "NO"

    confidence = max(1, min(5, round(score * 20)))

    # Plan de salida sugerido: no hace falta llegar a la resolución del mercado
    # para ganar — como cualquier libro de órdenes, se puede vender la acción
    # antes si el precio se mueve a favor. stop/target usan la volatilidad del
    # historial de precios (mismo principio que ATR_STOP_MULT/MIN_RR en cripto).
    entry_price = market["yes_price"] if direction == "YES" else market["no_price"]
    volatility = momentum_data["volatility"]
    trade_plan = None
    if volatility > 0:
        stop_distance = volatility * stop_vol_mult
        target_distance = stop_distance * target_rr
        trade_plan = {
            "entry": round(entry_price, 3),
            "target": round(min(0.99, entry_price + target_distance), 3),
            "stop": round(max(0.01, entry_price - stop_distance), 3),
        }
    
    return {
        "type": "POLYMARKET_SIGNAL",
        "direction": direction,
        "confidence": confidence,
        "score": score,
        "reasons": reasons,
        "market": {
            "question": market["question"],
            "condition_id": market["condition_id"],
            "yes_price": market["yes_price"],
            "no_price": market["no_price"],
            "volume_24h": market["volume_24h"],
            "liquidity": market["liquidity"],
            "days_to_resolution": days_to_resolution,
        },
        "momentum": momentum_data,
        "inefficiency": inefficiency_data,
        "trade_plan": trade_plan,
    }