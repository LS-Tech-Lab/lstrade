"""
Capa de conexión real al exchange vía ccxt. Esta es la única parte del núcleo
(compartido entre VPS y serverless) que habla con el mundo exterior.
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
        }
        if config.API_PASSWORD:
            params["password"] = config.API_PASSWORD
        self.exchange = exchange_class(params)

    def fetch_ohlcv(self, symbol, timeframe=None, limit=None):
        """Devuelve una lista de dicts [{"ts","o","h","l","c","v"}, ...], la más
        reciente al final. Sin pandas — livianito para cold-start serverless."""
        timeframe = timeframe or self.config.TIMEFRAME
        limit = limit or self.config.CANDLE_LIMIT
        raw = self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        return [
            {"ts": r[0], "o": r[1], "h": r[2], "l": r[3], "c": r[4], "v": r[5]}
            for r in raw
        ]

    def fetch_equity(self, quote_currency="USDT"):
        """Devuelve el equity total aproximado en la moneda de referencia (spot)."""
        balance = self.exchange.fetch_balance()
        total = balance.get("total", {})
        if quote_currency in total:
            # Suma quote + valor estimado de otros activos si el exchange lo expone en 'info'
            equity = total.get(quote_currency, 0.0)
            for asset, amount in total.items():
                if asset == quote_currency or amount in (0, None):
                    continue
                pair = f"{asset}/{quote_currency}"
                try:
                    ticker = self.exchange.fetch_ticker(pair)
                    equity += amount * ticker["last"]
                except Exception:
                    continue  # si no existe el par, se ignora ese activo en el cálculo
            return equity
        return sum(v for v in total.values() if isinstance(v, (int, float)))

    def create_order(self, symbol, side, amount, order_type="market", price=None):
        """Coloca una orden REAL en el exchange. side: 'buy' o 'sell'."""
        log.warning(f"[ORDEN REAL] {side.upper()} {amount} {symbol} ({order_type})")
        if order_type == "market":
            return self.exchange.create_order(symbol, "market", side, amount)
        else:
            return self.exchange.create_order(symbol, order_type, side, amount, price)

    def min_notional(self, symbol):
        """Intenta obtener el mínimo de orden permitido por el exchange para ese par."""
        try:
            markets = self.exchange.load_markets()
            m = markets.get(symbol, {})
            return m.get("limits", {}).get("cost", {}).get("min")
        except Exception:
            return None
