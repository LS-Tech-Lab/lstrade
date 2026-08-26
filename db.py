"""
Persistencia en SQLite con soporte para Open Trades (Trailing Stop).
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
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, equity REAL NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, symbol TEXT NOT NULL,
            signal_type TEXT, direction TEXT, confidence INTEGER, risk_pass INTEGER,
            risk_detail TEXT, plan_detail TEXT, decision TEXT, order_detail TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS state (
            key TEXT PRIMARY KEY, value TEXT)""")
        
        # NUEVO: Tabla para gestionar posiciones abiertas y Trailing Stop
        c.execute("""CREATE TABLE IF NOT EXISTS open_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            entry_price REAL NOT NULL,
            current_stop REAL NOT NULL,
            target_price REAL NOT NULL,
            position_size REAL NOT NULL,
            order_id TEXT,
            ts_opened REAL NOT NULL)""")
        self.conn.commit()

    def record_equity(self, equity):
        self.conn.execute("INSERT INTO equity_history (ts, equity) VALUES (?,?)", (time.time(), equity))
        self.conn.commit()

    def peak_equity(self):
        row = self.conn.execute("SELECT MAX(equity) as peak FROM equity_history").fetchone()
        return row["peak"] if row and row["peak"] is not None else None

    def current_exposure_pct(self, equity):
        cutoff = time.time() - 24 * 3600
        rows = self.conn.execute(
            "SELECT plan_detail FROM decisions WHERE decision IN ('approved','auto_executed') AND ts > ?", (cutoff,)
        ).fetchall()
        total_risk = sum(json.loads(r["plan_detail"]).get("risk_amount", 0.0) for r in rows if r["plan_detail"])
        return (total_risk / equity) * 100 if equity > 0 else 0.0

    def get_state(self, key, default=None):
        row = self.conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_state(self, key, value):
        self.conn.execute(
            "INSERT INTO state (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value))
        )
        self.conn.commit()

    def log_decision(self, symbol, signal, risk_report, plan, decision, order_detail=None):
        self.conn.execute(
            """INSERT INTO decisions (ts, symbol, signal_type, direction, confidence, risk_pass, risk_detail, plan_detail, decision, order_detail)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (time.time(), symbol, signal.get("type") if signal else None, signal.get("direction") if signal else None,
             signal.get("confidence") if signal else None, int(risk_report["pass"]) if risk_report else None,
             json.dumps(risk_report) if risk_report else None, json.dumps(plan) if plan else None, decision,
             json.dumps(order_detail) if order_detail else None)
        )
        self.conn.commit()

    # NUEVO: Métodos para Trailing Stop
    def add_open_trade(self, symbol, direction, entry_price, stop_price, target_price, position_size, order_id=None):
        self.conn.execute(
            """INSERT INTO open_trades (symbol, direction, entry_price, current_stop, target_price, position_size, order_id, ts_opened)
            VALUES (?,?,?,?,?,?,?,?)""",
            (symbol, direction, entry_price, stop_price, target_price, position_size, order_id, time.time())
        )
        self.conn.commit()

    def get_open_trades(self):
        return self.conn.execute("SELECT * FROM open_trades").fetchall()

    def update_trade_stop(self, trade_id, new_stop_price):
        self.conn.execute("UPDATE open_trades SET current_stop = ? WHERE id = ?", (new_stop_price, trade_id))
        self.conn.commit()

    def close_trade(self, trade_id):
        self.conn.execute("DELETE FROM open_trades WHERE id = ?", (trade_id,))
        self.conn.commit()