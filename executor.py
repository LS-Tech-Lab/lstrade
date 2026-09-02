"""
Ejecuta el plan: si LIVE_TRADING está apagado, todo queda en modo papel.
Si está prendido, coloca una orden REAL en el exchange y valida su llenado.
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

            # NUEVO (Semana 1): Validación de llenado parcial (Partial Fill).
            # Si la orden es límite o hay baja liquidez, puede no llenarse al 100%.
            # Verificamos el estado real para ajustar el stop-loss al tamaño ejecutado.
            effective_amount = amount
            if isinstance(order, dict) and order.get("id"):
                try:
                    order_status = self.exchange_client.fetch_order(order["id"], symbol)
                    actual_filled = float(order_status.get("filled", 0) or 0)
                    # FIX (auditoría 02/09/2026): la condición anterior era
                    # `actual_filled > 0 and actual_filled < amount * 0.95`,
                    # así que si la orden todavía tenía CERO llenado (ej.
                    # ORDER_TYPE=limit que no tocó precio todavía),
                    # effective_amount se quedaba en el `amount` original y
                    # más abajo se colocaba un stop-loss real por el tamaño
                    # completo sin tener NADA abierto en el exchange. Con
                    # ORDER_TYPE=market (default) casi nunca se daba porque
                    # el llenado es inmediato, pero es un stop huérfano
                    # garantizado apenas se use una orden límite.
                    if actual_filled == 0:
                        log.warning(f"Orden {symbol} sin llenar todavía (filled=0). Cancelando antes de colocar stop-loss.")
                        self.exchange_client.cancel_order(symbol, order["id"])
                        return {"mode": "live", "status": "unfilled_cancelled", "order": order}
                    if actual_filled < amount * 0.95:
                        log.warning(f"ALERTA: Llenado parcial en {symbol}. Esperado: {amount}, Llenado: {actual_filled}. Cancelando remanente.")
                        self.exchange_client.cancel_order(symbol, order["id"])
                        effective_amount = actual_filled
                        order["filled"] = actual_filled
                except Exception as e:
                    log.warning(f"No se pudo verificar el estado de la orden en {symbol} (se asume llenado completo): {e}")

            stop_order = None
            stop_error = None
            stop_side = "sell" if plan["direction"] == "LONG" else "buy"
            try:
                # Usamos effective_amount para que el stop cubra solo lo realmente llenado
                stop_order = self.exchange_client.create_stop_order(symbol, stop_side, effective_amount, plan["stop"])
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
