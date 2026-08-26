"""
Indicadores técnicos calculados sobre datos OHLCV reales — Python puro.
"""
import math

def _closes(candles):
    return [c["c"] for c in candles]

def sma(candles, window):
    closes = _closes(candles)
    if len(closes) < window:
        return None
    return sum(closes[-window:]) / window

def ema(candles, window):
    closes = _closes(candles)
    if len(closes) < window:
        return None
    k = 2 / (window + 1)
    value = closes[0]
    for price in closes[1:]:
        value = price * k + value * (1 - k)
    return value

def atr(candles, window=14):
    if len(candles) < window + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h, l, prev_c = candles[i]["h"], candles[i]["l"], candles[i - 1]["c"]
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)
    if len(trs) < window:
        return None
    return sum(trs[-window:]) / window

def rsi(candles, window=14):
    closes = _closes(candles)
    if len(closes) < window + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))
    avg_gain = sum(gains[-window:]) / window
    avg_loss = sum(losses[-window:]) / window
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def rolling_return_stdev(candles, window=10):
    closes = _closes(candles)
    if len(closes) < window + 1:
        return None
    returns = []
    tail = closes[-(window + 1):]
    for i in range(1, len(tail)):
        returns.append((tail[i] - tail[i - 1]) / tail[i - 1])
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / len(returns)
    return math.sqrt(variance)

# NUEVO: Filtro de volumen
def volume_ratio(candles, window=20):
    if len(candles) < window + 1:
        return None
    volumes = [c["v"] for c in candles]
    avg_volume = sum(volumes[-(window + 1):-1]) / window
    if avg_volume == 0:
        return None
    return volumes[-1] / avg_volume