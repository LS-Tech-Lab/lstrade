"""
Gestor de Posiciones y Trailing Stop Dinámico.
Se ejecuta al inicio de cada ciclo para proteger ganancias en operaciones activas.
"""
import logging

from format_utils import format_money, direction_label

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

                # AUDITORÍA (08/09/2026): los multiplicadores 1.0 (breakeven)
                # y 1.5 (distancia de persecución) estaban hardcodeados --
                # ahora son config.TRAIL_BREAKEVEN_ATR_MULT / TRAIL_ATR_MULT
                # (default 1.0 / 1.2, antes 1.0 / 1.5) para poder ajustar sin
                # redeploy y medir el efecto con analyze_crypto_setups.py.
                # Bajar TRAIL_ATR_MULT protege antes la ganancia ya hecha, a
                # costa de cortar antes algunos trades que hubieran seguido.
                breakeven_dist = atr * self.config.TRAIL_BREAKEVEN_ATR_MULT
                trail_dist = atr * self.config.TRAIL_ATR_MULT

                # Lógica de Trailing Stop
                if direction == "LONG":
                    # Si el precio subió más de breakeven_dist desde la entrada, movemos el stop a Breakeven
                    if current_price > entry + breakeven_dist and current_stop < entry:
                        new_stop = entry
                        moved = True
                    # Si ya está en breakeven, lo perseguimos a trail_dist del precio actual
                    elif current_price > entry + trail_dist:
                        trail_stop = current_price - trail_dist
                        if trail_stop > current_stop:
                            new_stop = trail_stop
                            moved = True
                else: # SHORT
                    if current_price < entry - breakeven_dist and current_stop > entry:
                        new_stop = entry
                        moved = True
                    elif current_price < entry - trail_dist:
                        trail_stop = current_price + trail_dist
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

                    # AUDITORÍA (06/09/2026): "Nuevo Stop: 0.123456" no decía
                    # qué implica el cambio — se agrega la frase en criollo
                    # y se usa format_money() (decimales adaptados a la
                    # magnitud del precio) en vez de 6 decimales fijos.
                    self.notifier.send_message(
                        f"🛡️ *Trailing Stop Actualizado* — {symbol} {direction_label(direction)}\n"
                        f"Nuevo stop: {format_money(new_stop)} — esto protege más ganancia "
                        f"si el precio sigue moviéndose a favor."
                    )
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

                    # FIX (carrera con /api/manage_positions): este método y
                    # run_manage_positions() en app.py son dos handlers
                    # independientes que pueden correr solapados (cron-job.org
                    # pega a /api/cycle mientras GitHub Actions dispara
                    # /api/manage_positions). Ambos leen open_trades y pueden
                    # detectar el mismo hit_target/hit_stop. close_trade_with_
                    # outcome() hace un DELETE ... y devuelve False si la fila
                    # ya no estaba (el otro handler la cerró primero) — hay
                    # que consultarlo ANTES de tocar el exchange o mandar
                    # Telegram, si no, este handler intenta cerrar a mercado
                    # una posición que el otro ya cerró (orden real duplicada
                    # / apertura accidental en el lado contrario) y manda un
                    # segundo aviso de "posición cerrada" con un r_multiple
                    # falso. Antes esto se llamaba DESPUÉS del bloque del
                    # exchange, así que nunca evitaba nada.
                    r_multiple = self.db.close_trade_with_outcome(trade, exit_price, outcome)
                    if r_multiple is False:
                        log.info(
                            f"[CIERRE] {symbol}: ya resuelto por otro handler "
                            f"(carrera con /api/manage_positions) — se omite duplicado."
                        )
                        continue

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

                    # FIX (07/09/2026): "won = outcome == target" estaba mal
                    # -- cuando el trailing stop ya había movido el stop a
                    # breakeven (ver más arriba: current_stop = entry), tocar
                    # el STOP no es una pérdida real: exit_price == entry,
                    # r_multiple da 0.0, y el mensaje decía "pérdida —
                    # Perdiste 0.0 veces", que es contradictorio (reportado
                    # en vivo: NEAR/USDT). El resultado real de la operación
                    # lo dice el signo de r_multiple, no cuál nivel (target o
                    # stop) fue el que se tocó -- son dos cosas distintas.
                    if r_multiple is None:
                        emoji, result_label, r_text = "🛑" if outcome == "stop" else "✅", "Resultado sin calcular", ""
                    elif r_multiple > 0.001:
                        emoji, result_label = "✅", "ganancia"
                        r_text = f" — Ganaste {r_multiple:.1f} veces lo que arriesgaste en esta operación"
                    elif r_multiple < -0.001:
                        emoji, result_label = "🛑", "pérdida"
                        r_text = f" — Perdiste {abs(r_multiple):.1f} veces lo que arriesgaste en esta operación"
                    else:
                        emoji, result_label = "⚪", "empate (breakeven)"
                        r_text = " — no ganaste ni perdiste: se cerró justo en el precio de entrada"
                    log.info(f"[CIERRE] {symbol} {direction}: {outcome} @ {exit_price:.6f} ({r_multiple})")
                    self.notifier.send_message(
                        f"{emoji} *Posición cerrada* — {symbol} {direction_label(direction)}\n"
                        f"Resultado: {result_label}{r_text}\n"
                        f"Precio de salida: {format_money(exit_price)}"
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
