"""
Ejecuta el plan: si LIVE_TRADING está apagado, todo queda en modo papel
(se registra como si se hubiera ejecutado, pero no se manda ninguna orden real).
Si está prendido, coloca una orden REAL en el exchange.
"""
import logging

log = logging.getLogger("executor")


class Executor:
    def __init__(self, exchange_client, config):
        self.exchange_client = exchange_client
        self.config = config

    def execute(self, symbol, plan):
        side = "buy" if plan["direction"] == "LONG" else "sell"
        amount = plan["position_size"]

        if not self.config.LIVE_TRADING:
            log.info(f"[PAPER] {side.upper()} {amount:.6f} {symbol} @ ~{plan['entry']}")
            return {"mode": "paper", "side": side, "amount": amount, "symbol": symbol, "status": "simulated"}

        try:
            min_cost = self.exchange_client.min_notional(symbol)
            notional = amount * plan["entry"]
            if min_cost and notional < min_cost:
                log.warning(f"Orden {symbol} por debajo del mínimo del exchange (${notional:.2f} < ${min_cost}). Cancelada.")
                return {"mode": "live", "status": "rejected_min_notional", "notional": notional, "min_cost": min_cost}

            order = self.exchange_client.create_order(symbol, side, amount, order_type=self.config.ORDER_TYPE)
            return {"mode": "live", "status": "filled", "order": order}
        except Exception as e:
            log.exception(f"Error ejecutando orden real en {symbol}: {e}")
            return {"mode": "live", "status": "error", "error": str(e)}
