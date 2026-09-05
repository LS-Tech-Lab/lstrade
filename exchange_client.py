"""
Cliente único para interactuar con el exchange vía ccxt.
Centraliza la conexión, el manejo de rate limits y los timeouts.
"""
import logging
import ccxt
from config import Config

log = logging.getLogger("exchange_client")

class ExchangeClient:
    def __init__(self, config):
        self.config = config
        exchange_class = getattr(ccxt, config.EXCHANGE_ID)
        params = {
            "apiKey": config.API_KEY,
            "secret": config.API_SECRET,
            "enableRateLimit": True,
            "timeout": config.EXCHANGE_TIMEOUT_MS,
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
        return self.exchange.fetch_ticker(symbol)

    # NUEVO (Semana 1): Permite verificar el estado real de una orden recién creada
    # para detectar y gestionar llenados parciales (Partial Fills).
    def fetch_order(self, order_id, symbol):
        return self.exchange.fetch_order(order_id, symbol)

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

    # AUDITORÍA (05/09/2026, categoría 12): create_order() y
    # create_stop_order() mandaban `amount`/`price`/`stopPrice` como floats
    # crudos calculados por risk_manager/trade_planner (division de
    # equity*risk_pct / stop_distance, aritmética de punto flotante común),
    # sin pasar por el redondeo de precisión real del exchange (tick size /
    # lot size de OKX para ese símbolo puntual). ccxt expone
    # amount_to_precision()/price_to_precision() justamente para esto --
    # sin ellos, cualquier orden cuyo amount/price tenga más decimales de
    # los que el par admite (ej. 0.123456789 BTC en un par que solo acepta
    # 6 decimales) puede ser rechazada por el exchange, o -- peor, según
    # cómo cada exchange trunque internamente -- ejecutada con un tamaño
    # ligeramente distinto al calculado por el motor de riesgo, sin que
    # nada en el bot se entere de la diferencia. No mordió todavía porque
    # LIVE_TRADING está en false (modo papel) desde que se configuró el
    # bot, pero es el primer punto a romper apenas se pase a operar real.
    # _to_precision() dispara load_markets() la primera vez si hace falta
    # (cacheado por ccxt de ahí en más), así que no agrega una llamada de
    # red extra en ciclos posteriores del mismo proceso.
    def _fmt_amount(self, symbol, amount):
        try:
            return float(self.exchange.amount_to_precision(symbol, amount))
        except Exception as e:
            log.warning(f"No se pudo aplicar precisión de amount para {symbol} ({e}); se usa el valor crudo {amount}.")
            return amount

    def _fmt_price(self, symbol, price):
        if price is None:
            return None
        try:
            return float(self.exchange.price_to_precision(symbol, price))
        except Exception as e:
            log.warning(f"No se pudo aplicar precisión de price para {symbol} ({e}); se usa el valor crudo {price}.")
            return price

    def create_order(self, symbol, side, amount, order_type="market", price=None):
        amount = self._fmt_amount(symbol, amount)
        price = self._fmt_price(symbol, price)
        log.warning(f"[ORDEN REAL] {side.upper()} {amount} {symbol} ({order_type})")
        if order_type == "market":
            return self.exchange.create_order(symbol, "market", side, amount)
        else:
            return self.exchange.create_order(symbol, order_type, side, amount, price)

    def create_stop_order(self, symbol, side, amount, stop_price):
        amount = self._fmt_amount(symbol, amount)
        stop_price = self._fmt_price(symbol, stop_price)
        log.info(f"[STOP ORDER] {side.upper()} {amount} {symbol} @ stop {stop_price}")
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
