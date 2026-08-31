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

            # NUEVO: antes acá terminaba todo — se mandaba la entrada y
            # nunca se colocaba ningún stop real en el exchange. La posición
            # quedaba desprotegida desde el momento en que se llenaba hasta
            # que position_manager.py decidiera moverla a breakeven (si es
            # que llegaba a moverse). Ahora se coloca el stop-loss real
            # inmediatamente después de que la entrada se confirma.
            stop_order = None
            stop_error = None
            stop_side = "sell" if plan["direction"] == "LONG" else "buy"
            try:
                stop_order = self.exchange_client.create_stop_order(symbol, stop_side, amount, plan["stop"])
            except Exception as e:
                stop_error = str(e)
                log.error(
                    f"ENTRADA en {symbol} se ejecutó (${notional:.2f}) pero el STOP-LOSS real "
                    f"NO se pudo colocar en el exchange: {e}. La posición está DESPROTEGIDA."
                )

            return {
                "mode": "live", "status": "filled", "order": order,
                "stop_order": stop_order, "stop_order_error": stop_error,
            }
        except Exception as e:
            log.exception(f"Error ejecutando orden real en {symbol}: {e}")
            return {"mode": "live", "status": "error", "error": str(e)}
