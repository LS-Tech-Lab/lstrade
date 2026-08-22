"""
Módulo de riesgo — la parte más importante del sistema.
Revisa exposición, drawdown y volatilidad contra el equity REAL de la cuenta
(traído del exchange, no simulado), y actúa como circuit breaker si el
drawdown supera el límite crítico configurado.
"""
import logging

log = logging.getLogger("risk_manager")


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

    def check(self, symbol, signal, equity):
        atr_val = signal["atr"]
        price = signal["price"]
        stop_distance = atr_val * self.config.ATR_STOP_MULT
        risk_amount = equity * (self.config.RISK_PCT_PER_TRADE / 100)
        position_size = risk_amount / stop_distance if stop_distance > 0 else 0

        peak = self.db.peak_equity() or equity
        dd_pct = ((peak - equity) / peak * 100) if peak > 0 else 0.0
        exposure_pct = self.db.current_exposure_pct(equity)
        vol_pct = signal["volatility"] * 100

        checks = [
            {"label": "Tamaño de posición calculable", "ok": stop_distance > 0 and position_size > 0},
            {"label": f"Exposición < {self.config.MAX_EXPOSURE_PCT}%", "ok": exposure_pct < self.config.MAX_EXPOSURE_PCT},
            {"label": f"Drawdown < {self.config.MAX_DRAWDOWN_PCT}%", "ok": dd_pct < self.config.MAX_DRAWDOWN_PCT},
            {"label": f"Volatilidad < {self.config.MAX_VOLATILITY_PCT}%", "ok": vol_pct < self.config.MAX_VOLATILITY_PCT},
            {"label": "Sistema no detenido por circuit breaker", "ok": not self.is_halted()},
        ]
        overall_pass = all(c["ok"] for c in checks)

        return {
            "pass": overall_pass,
            "checks": checks,
            "risk_amount": risk_amount,
            "position_size": position_size,
            "stop_distance": stop_distance,
            "exposure_pct": exposure_pct,
            "drawdown_pct": dd_pct,
            "volatility_pct": vol_pct,
        }
