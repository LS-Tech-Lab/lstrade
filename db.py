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
            ts_opened REAL NOT NULL,
            stop_distance REAL)""")

        # Columna nueva en bases ya existentes (SQLite no soporta
        # "ADD COLUMN IF NOT EXISTS", así que se intenta y se ignora si ya está).
        try:
            c.execute("ALTER TABLE open_trades ADD COLUMN stop_distance REAL")
        except sqlite3.OperationalError:
            pass

        # NUEVO: Trades cerrados con resultado — sin esto no había forma de
        # calcular win rate/expectancy reales sobre lo que pasó en producción,
        # solo sobre el backtest offline.
        c.execute("""CREATE TABLE IF NOT EXISTS closed_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            entry_price REAL NOT NULL,
            exit_price REAL NOT NULL,
            outcome TEXT NOT NULL,
            r_multiple REAL,
            ts_opened REAL NOT NULL,
            ts_closed REAL NOT NULL)""")

        # NUEVO: Señales de Polymarket con plan de salida, para poder medir
        # después si el target o el stop se tocaron primero — antes no había
        # ningún registro de resultado, solo deduplicación de avisos.
        c.execute("""CREATE TABLE IF NOT EXISTS polymarket_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            condition_id TEXT NOT NULL,
            question TEXT,
            direction TEXT NOT NULL,
            token_id TEXT NOT NULL,
            entry REAL NOT NULL,
            target REAL NOT NULL,
            stop REAL NOT NULL,
            ts_signaled REAL NOT NULL,
            outcome TEXT,
            exit_price REAL,
            ts_resolved REAL)""")

        # NUEVO: snapshot de indicadores por símbolo en cada ciclo — ver
        # schema.sql (modo Supabase) para el razonamiento completo.
        c.execute("""CREATE TABLE IF NOT EXISTS indicator_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            ts REAL NOT NULL,
            price REAL,
            rsi REAL,
            atr_pct REAL,
            volume_ratio REAL,
            volatility REAL,
            momentum REAL,
            trend_align REAL,
            trend_bias TEXT)""")
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
    def add_open_trade(self, symbol, direction, entry_price, stop_price, target_price, position_size,
                        order_id=None, stop_distance=None):
        if stop_distance is None:
            stop_distance = abs(entry_price - stop_price)
        self.conn.execute(
            """INSERT INTO open_trades
            (symbol, direction, entry_price, current_stop, target_price, position_size, order_id, ts_opened, stop_distance)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (symbol, direction, entry_price, stop_price, target_price, position_size, order_id, time.time(), stop_distance)
        )
        self.conn.commit()

    def get_open_trades(self):
        return self.conn.execute("SELECT * FROM open_trades").fetchall()

    def count_open_trades_by_direction(self, direction):
        """
        Cuántas posiciones abiertas ya van en la misma dirección (LONG/SHORT).
        Se usa como proxy simple de correlación: en cripto, la mayoría de las
        altcoins se mueven junto con BTC, así que varias posiciones LONG
        simultáneas suelen ser, en la práctica, una sola apuesta direccional
        concentrada — aunque estén repartidas en símbolos distintos.
        """
        row = self.conn.execute(
            "SELECT COUNT(*) as n FROM open_trades WHERE direction = ?", (direction,)
        ).fetchone()
        return row["n"] if row else 0

    def update_trade_stop(self, trade_id, new_stop_price):
        self.conn.execute("UPDATE open_trades SET current_stop = ? WHERE id = ?", (new_stop_price, trade_id))
        self.conn.commit()

    def close_trade(self, trade_id):
        self.conn.execute("DELETE FROM open_trades WHERE id = ?", (trade_id,))
        self.conn.commit()

    def close_trade_with_outcome(self, trade, exit_price, outcome):
        """
        Cierra una posición abierta Y registra el resultado en closed_trades
        (win/loss en R). Sin esto no había ninguna tabla que guardara qué pasó
        realmente con cada trade una vez que se abría — quedaba en open_trades
        para siempre o se borraba sin dejar rastro del resultado.
        """
        entry = trade["entry_price"]
        direction = trade["direction"]
        stop_distance = trade["stop_distance"] if trade["stop_distance"] else None
        r_multiple = None
        if stop_distance:
            sign = 1 if direction == "LONG" else -1
            r_multiple = ((exit_price - entry) / stop_distance) * sign

        self.conn.execute(
            """INSERT INTO closed_trades (symbol, direction, entry_price, exit_price, outcome, r_multiple, ts_opened, ts_closed)
            VALUES (?,?,?,?,?,?,?,?)""",
            (trade["symbol"], direction, entry, exit_price, outcome, r_multiple, trade["ts_opened"], time.time())
        )
        self.conn.execute("DELETE FROM open_trades WHERE id = ?", (trade["id"],))
        self.conn.commit()
        return r_multiple

    def get_closed_trades(self, limit=500):
        return self.conn.execute(
            "SELECT * FROM closed_trades ORDER BY ts_closed DESC LIMIT ?", (limit,)
        ).fetchall()

    def stats_by_symbol(self, since_ts=None):
        """Win rate/expectancy por símbolo, opcionalmente desde una fecha (epoch)."""
        query = "SELECT symbol, outcome, r_multiple FROM closed_trades WHERE r_multiple IS NOT NULL"
        params = ()
        if since_ts is not None:
            query += " AND ts_closed >= ?"
            params = (since_ts,)
        rows = self.conn.execute(query, params).fetchall()
        by_symbol = {}
        for r in rows:
            by_symbol.setdefault(r["symbol"], []).append(r)
        result = {}
        for symbol, trades in by_symbol.items():
            n = len(trades)
            wins = [t["r_multiple"] for t in trades if t["outcome"] == "target"]
            result[symbol] = {
                "n": n,
                "win_rate": len(wins) / n * 100,
                "expectancy_r": sum(t["r_multiple"] for t in trades) / n,
                "total_r": sum(t["r_multiple"] for t in trades),
            }
        return result

    # NUEVO: snapshot de indicadores por símbolo, independiente de si hubo
    # señal de trading — ver compute_indicator_snapshot() en signal_engine.py.
    def record_indicator_snapshot(self, symbol, snapshot):
        self.conn.execute(
            """INSERT INTO indicator_snapshots
            (symbol, ts, price, rsi, atr_pct, volume_ratio, volatility, momentum, trend_align, trend_bias)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (symbol, time.time(), snapshot.get("price"), snapshot.get("rsi"), snapshot.get("atr_pct"),
             snapshot.get("volume_ratio"), snapshot.get("volatility"), snapshot.get("momentum"),
             snapshot.get("trend_align"), snapshot.get("trend_bias"))
        )
        self.conn.commit()

    def latest_indicator_snapshots(self):
        rows = self.conn.execute(
            """SELECT s1.* FROM indicator_snapshots s1
            INNER JOIN (SELECT symbol, MAX(ts) as max_ts FROM indicator_snapshots GROUP BY symbol) s2
            ON s1.symbol = s2.symbol AND s1.ts = s2.max_ts"""
        ).fetchall()
        return [dict(r) for r in rows]

    # NUEVO: Tracking de resultados de señales de Polymarket
    def record_polymarket_signal(self, condition_id, question, direction, token_id, entry, target, stop):
        self.conn.execute(
            """INSERT INTO polymarket_signals
            (condition_id, question, direction, token_id, entry, target, stop, ts_signaled)
            VALUES (?,?,?,?,?,?,?,?)""",
            (condition_id, question, direction, token_id, entry, target, stop, time.time())
        )
        self.conn.commit()

    def get_open_polymarket_signals(self):
        return self.conn.execute(
            "SELECT * FROM polymarket_signals WHERE outcome IS NULL"
        ).fetchall()

    def resolve_polymarket_signal(self, signal_id, exit_price, outcome):
        self.conn.execute(
            "UPDATE polymarket_signals SET outcome=?, exit_price=?, ts_resolved=? WHERE id=?",
            (outcome, exit_price, time.time(), signal_id)
        )
        self.conn.commit()

    def polymarket_stats_summary(self):
        rows = self.conn.execute(
            "SELECT direction, entry, target, stop, outcome, exit_price FROM polymarket_signals WHERE outcome IS NOT NULL"
        ).fetchall()
        n = len(rows)
        if n == 0:
            return {"n": 0, "win_rate": None, "expectancy_r": None}
        r_multiples = []
        wins = 0
        for r in rows:
            stop_distance = abs(r["entry"] - r["stop"])
            if stop_distance <= 0:
                continue
            rm = (r["exit_price"] - r["entry"]) / stop_distance
            r_multiples.append(rm)
            if r["outcome"] == "target":
                wins += 1
        return {
            "n": n,
            "win_rate": wins / n * 100,
            "expectancy_r": sum(r_multiples) / len(r_multiples) if r_multiples else None,
        }

    def polymarket_stats_by_category(self):
        """
        Mismo desglose que analyze_polymarket_categories.py (backtest offline)
        pero sobre las señales de PRODUCCIÓN ya resueltas — usa la misma
        categorize() de polymarket_categories.py para que ambas lecturas
        coincidan.
        """
        from polymarket_categories import categorize

        rows = self.conn.execute(
            "SELECT question, entry, target, stop, outcome, exit_price FROM polymarket_signals WHERE outcome IS NOT NULL"
        ).fetchall()
        by_category = {}
        for r in rows:
            stop_distance = abs(r["entry"] - r["stop"])
            if stop_distance <= 0:
                continue
            rm = (r["exit_price"] - r["entry"]) / stop_distance
            cat = categorize(r["question"])
            by_category.setdefault(cat, []).append((rm, r["outcome"]))

        result = {}
        for cat, entries in by_category.items():
            n = len(entries)
            wins = sum(1 for _, outcome in entries if outcome == "target")
            gross_win = sum(rm for rm, _ in entries if rm > 0)
            gross_loss = abs(sum(rm for rm, _ in entries if rm < 0))
            result[cat] = {
                "n": n,
                "win_rate": wins / n * 100,
                "expectancy_r": sum(rm for rm, _ in entries) / n,
                "total_r": sum(rm for rm, _ in entries),
                "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
            }
        return result

    def stats_summary(self, since_ts=None):
        """Win rate, expectancy y profit factor reales sobre trades ya cerrados."""
        query = "SELECT outcome, r_multiple FROM closed_trades WHERE r_multiple IS NOT NULL"
        params = ()
        if since_ts is not None:
            query += " AND ts_closed >= ?"
            params = (since_ts,)
        rows = self.conn.execute(query, params).fetchall()
        n = len(rows)
        if n == 0:
            return {"n": 0, "win_rate": None, "expectancy_r": None, "profit_factor": None}
        wins = [r["r_multiple"] for r in rows if r["outcome"] == "target"]
        losses = [r["r_multiple"] for r in rows if r["outcome"] == "stop"]
        gross_win = sum(r for r in wins if r > 0)
        gross_loss = abs(sum(r for r in losses if r < 0))
        return {
            "n": n,
            "win_rate": len(wins) / n * 100,
            "expectancy_r": sum(r["r_multiple"] for r in rows) / n,
            "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
        }