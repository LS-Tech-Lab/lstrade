"""
Módulo de riesgo — con filtro de Spread/Liquidez añadido.
"""
import logging

log = logging.getLogger("risk_manager")


def adaptive_atr_stop_mult(config, volatility_pct):
    """
    Múltiplo de ATR para el stop, escalado por régimen de volatilidad si
    ADAPTIVE_ATR_STOP está activo (ver config.py). Compartido entre
    risk_manager.check() (producción) y backtest.py, para que el backtest
    mida exactamente lo mismo que corre en vivo.
    """
    if not getattr(config, "ADAPTIVE_ATR_STOP", False):
        return config.ATR_STOP_MULT
    ref = getattr(config, "ATR_STOP_VOL_REF_PCT", 1.0) or 1.0
    scale = volatility_pct / ref if ref > 0 else 1.0
    mult = config.ATR_STOP_MULT * scale
    return max(config.ATR_STOP_MULT_MIN, min(config.ATR_STOP_MULT_MAX, mult))


def format_blocked_message(symbol, signal, failed_checks):
    """
    Arma el mensaje de Telegram para una señal bloqueada por riesgo.
    Antes era una sola línea con todos los checks fallidos pegados por
    coma (`', '.join(failed)`) — ilegible en el celular apenas fallaba más
    de un check, y sin contexto de la señal (había que scrollear al mensaje
    anterior de "Señal detectada" para ver dirección/confianza/precio).
    Ahora: un check fallido por línea, más el contexto de la señal arriba.
    Centralizado acá porque los tres entrypoints (app.py, main.py,
    api/cycle.py) mandaban este mensaje por separado con el mismo texto.
    """
    stars = "★" * signal.get("confidence", 0)
    price = signal.get("price")
    price_str = f"{price:,.6g}" if isinstance(price, (int, float)) else "—"
    lines = [
        f"\u26D4 *{symbol} bloqueado por riesgo*",
        f"{signal.get('type', '—')} · {signal.get('direction', '—')} · Confianza {stars or '—'} · Precio {price_str}",
        "",
    ]
    lines.extend(f"\u2715 {label}" for label in failed_checks)
    return "\n".join(lines)


class RiskManager:
    def __init__(self, config, db):
        self.config = config
        self.db = db

    def is_halted(self):
        return self.db.get_state("trading_halted", "0") == "1"

    def halt(self, reason):
        self.db.set_state("trading_halted", "1")
        self.db.set_state("halt_reason", reason)
        log.error(f"CIRCUIT BREAKER ACTIVADO: {reason}. El sistema no operará hasta reinicio manual.")

    def manual_reset(self):
        self.db.set_state("trading_halted", "0")
        self.db.set_state("halt_reason", "")

    def update_equity_and_check_kill_switch(self, equity):
        self.db.record_equity(equity)
        peak = self.db.peak_equity() or equity
        dd_pct = ((peak - equity) / peak * 100) if peak > 0 else 0.0
        if dd_pct >= self.config.MAX_DRAWDOWN_KILL_PCT and not self.is_halted():
            self.halt(f"Drawdown {dd_pct:.2f}% superó el límite crítico de {self.config.MAX_DRAWDOWN_KILL_PCT}%")
        return dd_pct

    def check(self, symbol, signal, equity, ticker=None):
        atr_val = signal["atr"]
        price = signal["price"]
        stop_mult = adaptive_atr_stop_mult(self.config, signal["volatility"] * 100)
        stop_distance = atr_val * stop_mult
        risk_amount = equity * (self.config.RISK_PCT_PER_TRADE / 100)
        position_size = risk_amount / stop_distance if stop_distance > 0 else 0
        
        peak = self.db.peak_equity() or equity
        dd_pct = ((peak - equity) / peak * 100) if peak > 0 else 0.0
        exposure_pct = self.db.current_exposure_pct(equity)
        vol_pct = signal["volatility"] * 100
        
        # NUEVO: las etiquetas ahora incluyen el valor actual, no solo el
        # umbral — antes decían por ejemplo "Exposición < 20%" tanto si
        # pasaba como si fallaba, así que un check bloqueado no decía por
        # cuánto se pasó (¿19.9% o 45%?). Eso obligaba a ir a mirar los
        # campos sueltos de risk_report en vez de leer el motivo solo.
        checks = [
            {"label": "Tamaño de posición calculable", "ok": stop_distance > 0 and position_size > 0},
            {"label": f"Exposición: {exposure_pct:.1f}% < {self.config.MAX_EXPOSURE_PCT}%", "ok": exposure_pct < self.config.MAX_EXPOSURE_PCT},
            {"label": f"Drawdown: {dd_pct:.1f}% < {self.config.MAX_DRAWDOWN_PCT}%", "ok": dd_pct < self.config.MAX_DRAWDOWN_PCT},
            {"label": f"Volatilidad: {vol_pct:.2f}% < {self.config.MAX_VOLATILITY_PCT}%", "ok": vol_pct < self.config.MAX_VOLATILITY_PCT},
            {"label": "Sistema no detenido por circuit breaker", "ok": not self.is_halted()},
        ]
        
        # NUEVO: antes esto era "ok": True con el comentario "fallo seguro"
        # — pero aprobar automáticamente cuando FALTAN los datos es fail-OPEN,
        # no fail-safe. Un chequeo de riesgo que no puede verificarse debe
        # bloquear, no pasar de largo. (Y antes de esto, app.py ni siquiera
        # pasaba `ticker`, así que esta rama corría siempre — el spread
        # nunca bloqueó nada; ver el fix en app.py que ahora sí lo trae.)
        if ticker and "bid" in ticker and "ask" in ticker and ticker["bid"] > 0:
            spread_pct = ((ticker["ask"] - ticker["bid"]) / ticker["bid"]) * 100
            checks.append({"label": f"Spread: {spread_pct:.2f}% < {self.config.MAX_SPREAD_PCT}%", "ok": spread_pct < self.config.MAX_SPREAD_PCT})
        else:
            checks.append({"label": "Spread (datos no disponibles — bloqueado por seguridad)", "ok": False})

        # NUEVO: Exposición correlacionada — evita que varias posiciones en la
        # misma dirección (LONG o SHORT), aunque sean símbolos distintos,
        # terminen siendo una sola apuesta concentrada disfrazada de cartera
        # diversificada. Ver MAX_CORRELATED_POSITIONS en config.py.
        correlated_count = self.db.count_open_trades_by_direction(signal["direction"])
        checks.append({
            "label": f"Posiciones correlacionadas ({signal['direction']}): {correlated_count} < {self.config.MAX_CORRELATED_POSITIONS}",
            "ok": correlated_count < self.config.MAX_CORRELATED_POSITIONS,
        })

        overall_pass = all(c["ok"] for c in checks)
        return {
            "pass": overall_pass,
            "checks": checks,
            "risk_amount": risk_amount,
            "position_size": position_size,
            "stop_distance": stop_distance,
            "atr_stop_mult": stop_mult,
            "exposure_pct": exposure_pct,
            "drawdown_pct": dd_pct,
            "volatility_pct": vol_pct,
        }
