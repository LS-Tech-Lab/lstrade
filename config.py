"""
Configuración central de Trader IA 24/7.
"""
import os
from dotenv import load_dotenv

load_dotenv()

def _bool(name, default=False):
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")

def _float(name, default):
    val = os.getenv(name)
    return float(val) if val not in (None, "") else default

def _int(name, default):
    val = os.getenv(name)
    return int(val) if val not in (None, "") else default

class Config:
    EXCHANGE_ID = os.getenv("EXCHANGE_ID", "binance")
    API_KEY = os.getenv("API_KEY", "")
    API_SECRET = os.getenv("API_SECRET", "")
    API_PASSWORD = os.getenv("API_PASSWORD", "")

    SYMBOLS = [s.strip() for s in os.getenv("SYMBOLS", "BTC/USDT,ETH/USDT").split(",") if s.strip()]
    TIMEFRAME = os.getenv("TIMEFRAME", "1h")
    CANDLE_LIMIT = _int("CANDLE_LIMIT", 100)

    RISK_PCT_PER_TRADE = _float("RISK_PCT_PER_TRADE", 1.0)
    MAX_EXPOSURE_PCT = _float("MAX_EXPOSURE_PCT", 20.0)
    MAX_DRAWDOWN_PCT = _float("MAX_DRAWDOWN_PCT", 8.0)
    MAX_DRAWDOWN_KILL_PCT = _float("MAX_DRAWDOWN_KILL_PCT", 15.0)
    MAX_VOLATILITY_PCT = _float("MAX_VOLATILITY_PCT", 4.0)
    ATR_STOP_MULT = _float("ATR_STOP_MULT", 1.5)
    MIN_RR = _float("MIN_RR", 1.8)
    
    # NUEVO: Filtro de Spread/Liquidez
    MAX_SPREAD_PCT = _float("MAX_SPREAD_PCT", 0.5)  # Máximo 0.5% de diferencia entre bid y ask

    LIVE_TRADING = _bool("LIVE_TRADING", False)
    AUTO_EXECUTE = _bool("AUTO_EXECUTE", False)
    ORDER_TYPE = os.getenv("ORDER_TYPE", "market")

    LOOP_INTERVAL_SECONDS = _int("LOOP_INTERVAL_SECONDS", 300)
    DB_PATH = os.getenv("DB_PATH", "trader_ia_247.db")

    # Horas mínimas antes de reavisar la misma señal de Polymarket si no cambió
    # de dirección ni subió de score de forma relevante (evita spam en Telegram)
    POLYMARKET_RESEND_COOLDOWN_HOURS = _float("POLYMARKET_RESEND_COOLDOWN_HOURS", 6.0)

    # Plan de salida sugerido para señales de Polymarket: mismo principio que
    # ATR_STOP_MULT/MIN_RR del módulo cripto, pero usando la volatilidad del
    # historial de precios de Polymarket en vez de ATR. Sin validar todavía
    # contra resultados reales — punto de partida, no un valor probado.
    POLYMARKET_STOP_VOL_MULT = _float("POLYMARKET_STOP_VOL_MULT", 3.0)
    POLYMARKET_TARGET_RR = _float("POLYMARKET_TARGET_RR", 1.5)

    NOTIFY_TELEGRAM = _bool("NOTIFY_TELEGRAM", False)
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
    APPROVAL_TIMEOUT_SECONDS = _int("APPROVAL_TIMEOUT_SECONDS", 900)

    @classmethod
    def validate(cls):
        problems = []
        if cls.LIVE_TRADING and (not cls.API_KEY or not cls.API_SECRET):
            problems.append("LIVE_TRADING=true pero falta API_KEY/API_SECRET en .env")
        if not cls.SYMBOLS:
            problems.append("SYMBOLS está vacío")
        if cls.NOTIFY_TELEGRAM and (not cls.TELEGRAM_BOT_TOKEN or not cls.TELEGRAM_CHAT_ID):
            problems.append("NOTIFY_TELEGRAM=true pero falta TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID en .env")
        return problems