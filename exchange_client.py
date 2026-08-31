"""
Capa de conexión real al exchange vía ccxt.
"""
import ccxt
import logging

log = logging.getLogger("exchange_client")

class ExchangeClient:
    def __init__(self, config):
        self.config = config
        exchange_class = getattr(ccxt, config.EXCHANGE_ID)
        params = {
            "apiKey": config.API_KEY,
            "secret": config.API_SECRET,
            "enableRateLimit": True,
            # NUEVO: sin esto, ccxt espera hasta su default (10s) por cada
            # request HTTP — ver el comentario largo en config.py
            # (EXCHANGE_TIMEOUT_MS) sobre por qué eso importa en serverless.
            "timeout": config.EXCHANGE_TIMEOUT_MS,
            # NUEVO: ccxt llama automáticamente a fetch_currencies() dentro
            # de load_markets() (que a su vez dispara fetch_ohlcv() en el
            # primer uso) CUANDO hay API keys configuradas — sin importar
            # que no las necesitemos para leer velas públicas. En Binance
            # eso pega contra el endpoint privado sapi/v1/capital/config/getall,
            # que Binance bloquea con 451 "restricted location" según la
            # región del servidor (confirmado en logs reales: pasa desde
            # Vercel aunque las velas públicas sí son accesibles). Esto
            # apaga esa llamada puntual sin tocar fetch_ohlcv/fetch_ticker,
            # que usan endpoints públicos y no se ven afectados.
            "options": {"fetchCurrencies": False},
        }
        if config.API_PASSWORD:
            params["password"] = config.API_PASSWORD
        self.exchange = exchange_class(params)

    def fetch_ohlcv(self, symbol, timeframe=None, limit=None, since=None):
        timeframe = timeframe or self.config.TIMEFRAME
        limit = limit or self.config.CANDLE_LIMIT
        raw = self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit, since=since)
        return [
            {"ts": r[0], "o": r[1], "h": r[2], "l": r[3], "c": r[4], "v": r[5]}
            for r in raw
        ]

    def fetch_ticker(self, symbol):
        """Obtiene bid/ask para calcular el spread."""
        return self.exchange.fetch_ticker(symbol)

    def fetch_equity(self, quote_currency="USDT"):
        balance = self.exchange.fetch_balance()
        total = balance.get("total", {})
        if quote_currency in total:
            equity = total.get(quote_currency, 0.0)
            for asset, amount in total.items():
                if asset == quote_currency or amount in (0, None):
                    continue
                pair = f"{asset}/{quote_currency}"
                try:
                    ticker = self.exchange.fetch_ticker(pair)
                    equity += amount * ticker["last"]
                except Exception:
                    continue
            return equity
        return sum(v for v in total.values() if isinstance(v, (int, float)))

    def create_order(self, symbol, side, amount, order_type="market", price=None):
        log.warning(f"[ORDEN REAL] {side.upper()} {amount} {symbol} ({order_type})")
        if order_type == "market":
            return self.exchange.create_order(symbol, "market", side, amount)
        else:
            return self.exchange.create_order(symbol, order_type, side, amount, price)

    # NUEVO: Para Trailing Stop
    def create_stop_order(self, symbol, side, amount, stop_price):
        """Crea una orden de stop market/limit para proteger la posición."""
        log.info(f"[STOP ORDER] {side.upper()} {amount} {symbol} @ stop {stop_price}")
        # Binance spot soporta 'stop_loss' o 'stop' como order_type en ccxt
        return self.exchange.create_order(symbol, "stop_loss", side, amount, None, {"stopPrice": stop_price})

    def cancel_order(self, symbol, order_id):
        try:
            return self.exchange.cancel_order(order_id, symbol)
        except Exception as e:
            log.warning(f"No se pudo cancelar orden {order_id}: {e}")
            return None

    def min_notional(self, symbol):
        try:
            markets = self.exchange.load_markets()
            m = markets.get(symbol, {})
            return m.get("limits", {}).get("cost", {}).get("min")
        except Exception:
            return None
