"""
Gestor de Posiciones y Trailing Stop Dinámico.
Se ejecuta al inicio de cada ciclo para proteger ganancias en operaciones activas.
"""
import logging

log = logging.getLogger("position_manager")

class PositionManager:
    def __init__(self, config, db, exchange_client, notifier):
        self.config = config
        self.db = db
        self.exchange = exchange_client
        self.notifier = notifier

    def manage_open_positions(self):
        """Revisa todas las posiciones abiertas y aplica Trailing Stop."""
        open_trades = self.db.get_open_trades()
        if not open_trades:
            return

        log.info(f"Gestionando {len(open_trades)} posiciones abiertas (Trailing Stop)...")
        
        for trade in open_trades:
            symbol = trade["symbol"]
            direction = trade["direction"]
            entry = trade["entry_price"]
            current_stop = trade["current_stop"]
            position_size = trade["position_size"]
            trade_id = trade["id"]
            
            try:
                ticker = self.exchange.fetch_ticker(symbol)
                current_price = ticker["last"]
                atr = self._get_atr_for_symbol(symbol)
                
                if not atr:
                    continue

                new_stop = current_stop
                moved = False

                # Lógica de Trailing Stop
                if direction == "LONG":
                    # Si el precio subió más de 1 ATR desde la entrada, movemos el stop a Breakeven
                    if current_price > entry + atr and current_stop < entry:
                        new_stop = entry
                        moved = True
                    # Si ya está en breakeven, lo perseguimos a 1.5 ATR del precio actual
                    elif current_price > entry + (atr * 1.5):
                        trail_stop = current_price - (atr * 1.5)
                        if trail_stop > current_stop:
                            new_stop = trail_stop
                            moved = True
                else: # SHORT
                    if current_price < entry - atr and current_stop > entry:
                        new_stop = entry
                        moved = True
                    elif current_price < entry - (atr * 1.5):
                        trail_stop = current_price + (atr * 1.5)
                        if trail_stop < current_stop:
                            new_stop = trail_stop
                            moved = True

                if moved:
                    log.info(f"[TRAILING STOP] {symbol} {direction}: Stop movido de {current_stop:.6f} a {new_stop:.6f}")
                    self.db.update_trade_stop(trade_id, new_stop)
                    
                    if self.config.LIVE_TRADING and trade["order_id"]:
                        # En LIVE: Cancelar la orden de stop anterior y crear una nueva
                        self.exchange.cancel_order(symbol, trade["order_id"])
                        side = "sell" if direction == "LONG" else "buy"
                        new_order = self.exchange.create_stop_order(symbol, side, position_size, new_stop)
                        # Actualizar el order_id en la DB (simplificado, requeriría un método update_order_id)
                    
                    self.notifier.send_message(f"🛡️ *Trailing Stop Actualizado*\n{symbol} {direction}\nNuevo Stop: `{new_stop:.6f}`")

            except Exception as e:
                log.warning(f"Error gestionando posición {symbol}: {e}")

    def _get_atr_for_symbol(self, symbol):
        try:
            candles = self.exchange.fetch_ohlcv(symbol, timeframe=self.config.TIMEFRAME, limit=20)
            import indicators as ind
            return ind.atr(candles, 14)
        except Exception:
            return None