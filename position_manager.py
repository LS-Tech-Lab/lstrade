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

    def manage_open_positions(self, time_left_fn=None):
        """Revisa todas las posiciones abiertas y aplica Trailing Stop.

        FIX (02/09/2026): cada posición hace 2 llamadas de red sin límite
        (fetch_ticker + fetch_ohlcv para el ATR), y este método corría ANTES
        de que run_cycle() empezara a contar su time budget -- con varias
        posiciones abiertas eso solo ya podía comerse varios segundos que
        nunca se descontaban de ningún lado, y el ciclo terminaba pasándose
        del maxDuration de Vercel. Ahora acepta la misma función time_left()
        que usa el loop de escaneo en run_cycle(), y si queda poco tiempo
        corta el loop y deja las posiciones restantes para el próximo ciclo
        (no pasa nada por posponer un chequeo de trailing stop un rato en
        modo papel/LIVE_TRADING=false). time_left_fn es opcional para no
        romper otros callers (ej. main.py en modo VPS, que no tiene este
        límite de duración)."""
        open_trades = self.db.get_open_trades()
        if not open_trades:
            return

        log.info(f"Gestionando {len(open_trades)} posiciones abiertas (Trailing Stop)...")

        for trade in open_trades:
            if time_left_fn and time_left_fn() < 2.0:
                remaining = len(open_trades) - open_trades.index(trade)
                log.warning(
                    f"[SIN TIEMPO] Se corta la gestión de posiciones abiertas — "
                    f"quedaban {remaining} sin revisar, se retoman en el próximo ciclo."
                )
                break
            symbol = trade["symbol"]
            direction = trade["direction"]
            entry = trade["entry_price"]
            current_stop = trade["current_stop"]
            target_price = trade["target_price"]
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

                    new_order_id = None
                    if self.config.LIVE_TRADING and trade["order_id"]:
                        # En LIVE: Cancelar la orden de stop anterior y crear una nueva
                        try:
                            self.exchange.cancel_order(symbol, trade["order_id"])
                        except Exception as e:
                            log.warning(f"No se pudo cancelar la orden de stop anterior de {symbol} ({trade['order_id']}): {e}")
                        side = "sell" if direction == "LONG" else "buy"
                        try:
                            new_order = self.exchange.create_stop_order(symbol, side, position_size, new_stop)
                            new_order_id = new_order.get("id") if isinstance(new_order, dict) else None
                        except Exception as e:
                            log.warning(f"No se pudo crear la nueva orden de stop para {symbol}: {e}")
                            self.notifier.send_message(
                                f"\u26A0\uFE0F {symbol}: el trailing stop se movió en la base de datos "
                                f"pero la orden real en el exchange NO se pudo recrear — revisar a mano."
                            )

                    # NUEVO: se guarda el order_id nuevo (o se limpia si falló
                    # crearlo) — antes esto se perdía siempre, ver db.py.
                    self.db.update_trade_stop(trade_id, new_stop, new_order_id=new_order_id if self.config.LIVE_TRADING else None)
                    trade["order_id"] = new_order_id if self.config.LIVE_TRADING else trade["order_id"]

                    self.notifier.send_message(f"🛡️ *Trailing Stop Actualizado*\n{symbol} {direction}\nNuevo Stop: `{new_stop:.6f}`")
                    current_stop = new_stop

                # NUEVO: detectar si el precio ya cruzó el stop o el target.
                # Antes esto no se chequeaba nunca acá — una posición podía
                # quedar en open_trades indefinidamente (o solo se enteraba
                # si LIVE_TRADING tenía una orden de stop real en el exchange
                # que la ejecutara del otro lado), y en modo papel nunca se
                # cerraba ni quedaba registro de si ganó o perdió. Sin esto no
                # había forma de calcular win rate/expectancy real.
                hit_target = (current_price >= target_price) if direction == "LONG" else (current_price <= target_price)
                hit_stop = (current_price <= current_stop) if direction == "LONG" else (current_price >= current_stop)

                if hit_target or hit_stop:
                    outcome = "target" if hit_target else "stop"
                    exit_price = target_price if hit_target else current_stop

                    # NUEVO: antes esto solo cerraba a mercado en el exchange
                    # cuando outcome=="target" — si tocaba el STOP, la
                    # posición real quedaba abierta y desprotegida (la DB
                    # decía "cerrada" pero el exchange no se enteraba). Ahora
                    # se cierra en los dos casos. Para "stop" es además una
                    # red de seguridad: si el stop real ya se ejecutó solo en
                    # el exchange (colocado al entrar, ver executor.py), este
                    # intento adicional falla solo (ej. "insufficient
                    # balance") porque ya no queda nada que cerrar.
                    if self.config.LIVE_TRADING and trade["order_id"]:
                        try:
                            self.exchange.cancel_order(symbol, trade["order_id"])
                        except Exception:
                            pass
                        side = "sell" if direction == "LONG" else "buy"
                        try:
                            self.exchange.create_order(symbol, side, position_size, order_type="market")
                        except Exception as e:
                            log.warning(f"No se pudo cerrar {symbol} en el exchange al tocar {outcome}: {e}")
                            self.notifier.send_message(
                                f"⚠️ {symbol}: {outcome} detectado pero el cierre real en el "
                                f"exchange FALLÓ ({e}) — revisar la posición a mano."
                            )

                    r_multiple = self.db.close_trade_with_outcome(trade, exit_price, outcome)
                    emoji = "✅" if outcome == "target" else "🛑"
                    r_text = f" ({r_multiple:+.2f}R)" if r_multiple is not None else ""
                    log.info(f"[CIERRE] {symbol} {direction}: {outcome} @ {exit_price:.6f}{r_text}")
                    self.notifier.send_message(
                        f"{emoji} *Posición cerrada* — {symbol} {direction}\n"
                        f"Resultado: {outcome.upper()}{r_text}\nSalida: `{exit_price:.6f}`"
                    )

            except Exception as e:
                log.warning(f"Error gestionando posición {symbol}: {e}")

    def _get_atr_for_symbol(self, symbol):
        try:
            candles = self.exchange.fetch_ohlcv(symbol, timeframe=self.config.TIMEFRAME, limit=20)
            import indicators as ind
            return ind.atr(candles, 14)
        except Exception:
            return None
