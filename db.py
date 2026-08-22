"""
Persistencia en SQLite. Guarda todo lo necesario para calcular drawdown real,
exposición real, y llevar la bitácora de decisiones — no en memoria, sino en disco,
para que sobreviva reinicios (importante en un sistema que corre 24/7).
"""
import sqlite3
import time
import json


class Database:
    def __init__(self, path):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self):
        c = self.conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS equity_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            equity REAL NOT NULL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            symbol TEXT NOT NULL,
            signal_type TEXT,
            direction TEXT,
            confidence INTEGER,
            risk_pass INTEGER,
            risk_detail TEXT,
            plan_detail TEXT,
            decision TEXT,        -- approved / rejected / watchlist / blocked / auto_executed
            order_detail TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS state (
            key TEXT PRIMARY KEY,
            value TEXT
        )""")
        self.conn.commit()

    # --- equity / drawdown ---
    def record_equity(self, equity):
        self.conn.execute("INSERT INTO equity_history (ts, equity) VALUES (?,?)", (time.time(), equity))
        self.conn.commit()

    def peak_equity(self):
        row = self.conn.execute("SELECT MAX(equity) as peak FROM equity_history").fetchone()
        return row["peak"] if row and row["peak"] is not None else None

    def current_exposure_pct(self, equity):
        """Exposición aproximada: suma de riesgo comprometido en decisiones 'approved'
        de las últimas 24h que aún no fueron cerradas manualmente en la bitácora."""
        cutoff = time.time() - 24 * 3600
        rows = self.conn.execute(
            "SELECT plan_detail FROM decisions WHERE decision IN ('approved','auto_executed') AND ts > ?",
            (cutoff,)
        ).fetchall()
        total_risk = 0.0
        for r in rows:
            try:
                plan = json.loads(r["plan_detail"])
                total_risk += plan.get("risk_amount", 0.0)
            except Exception:
                continue
        if equity <= 0:
            return 0.0
        return (total_risk / equity) * 100

    # --- estado (circuit breaker, halted, etc) ---
    def get_state(self, key, default=None):
        row = self.conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_state(self, key, value):
        self.conn.execute(
            "INSERT INTO state (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value))
        )
        self.conn.commit()

    # --- bitácora ---
    def log_decision(self, symbol, signal, risk_report, plan, decision, order_detail=None):
        self.conn.execute(
            """INSERT INTO decisions
            (ts, symbol, signal_type, direction, confidence, risk_pass, risk_detail, plan_detail, decision, order_detail)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                time.time(), symbol,
                signal.get("type") if signal else None,
                signal.get("direction") if signal else None,
                signal.get("confidence") if signal else None,
                int(risk_report["pass"]) if risk_report else None,
                json.dumps(risk_report) if risk_report else None,
                json.dumps(plan) if plan else None,
                decision,
                json.dumps(order_detail) if order_detail else None,
            )
        )
        self.conn.commit()

    def recent_decisions(self, limit=20):
        return self.conn.execute(
            "SELECT * FROM decisions ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
