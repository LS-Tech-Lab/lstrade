"""
Persistencia en SQLite con soporte para Open Trades (Trailing Stop).
"""
import sqlite3
import time
import json

from weather_signal_engine import _half_kelly_fraction
from config import Config

class Database:
    def __init__(self, path):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self):
        c = self.conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS equity_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, equity REAL NOT NULL,
            module TEXT NOT NULL DEFAULT 'crypto')""")
        # AUDITORÍA (07/09/2026): columna `module` en bases ya existentes --
        # mismo motivo/mecanismo que el ALTER TABLE de stop_distance de abajo
        # (SQLite no soporta "ADD COLUMN IF NOT EXISTS").
        try:
            c.execute("ALTER TABLE equity_history ADD COLUMN module TEXT NOT NULL DEFAULT 'crypto'")
        except sqlite3.OperationalError:
            pass
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

        # AUDITORÍA (08/09/2026): setup_type/confidence/score -- mismo motivo
        # y mecanismo que el ALTER de stop_distance de arriba. Sin esto no
        # hay forma de desglosar win rate/expectancy por tipo de setup o
        # nivel de confianza (ver analyze_crypto_setups.py y el mismo cambio
        # en schema.sql/supabase_db.py para la base de producción).
        for col, coltype in (("setup_type", "TEXT"), ("confidence", "INTEGER"), ("score", "REAL")):
            try:
                c.execute(f"ALTER TABLE open_trades ADD COLUMN {col} {coltype}")
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
            ts_closed REAL NOT NULL,
            setup_type TEXT,
            confidence INTEGER,
            score REAL)""")

        for col, coltype in (("setup_type", "TEXT"), ("confidence", "INTEGER"), ("score", "REAL")):
            try:
                c.execute(f"ALTER TABLE closed_trades ADD COLUMN {col} {coltype}")
            except sqlite3.OperationalError:
                pass

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
            ts_resolved REAL,
            score REAL,
            confidence INTEGER)""")

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

        # NUEVO: señales de clima con la probabilidad estimada por el modelo,
        # para poder medir después (weather_track_results.py) si esa
        # probabilidad estuvo bien calibrada contra lo que realmente pasó —
        # antes no había ningún registro, solo deduplicación de avisos
        # (WeatherNotifyStateStore), así que no había forma de saber si el
        # modelo de clima acierta lo que dice acertar.
        c.execute("""CREATE TABLE IF NOT EXISTS weather_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            condition_id TEXT NOT NULL,
            question TEXT,
            event_title TEXT,
            station_icao TEXT,
            my_prob REAL NOT NULL,
            market_price REAL NOT NULL,
            ev REAL,
            center_estimate_f REAL,
            sigma REAL,
            yes_token_id TEXT,
            target_date TEXT,
            trajectory_slope_f_per_hr REAL,
            actual_high_f REAL,
            ts_signaled REAL NOT NULL,
            outcome TEXT,
            ts_resolved REAL)""")
        self.conn.commit()

    def record_equity(self, equity, module="crypto"):
        self.conn.execute("INSERT INTO equity_history (ts, equity, module) VALUES (?,?,?)", (time.time(), equity, module))
        self.conn.commit()

    def peak_equity(self, module="crypto"):
        row = self.conn.execute("SELECT MAX(equity) as peak FROM equity_history WHERE module = ?", (module,)).fetchone()
        return row["peak"] if row and row["peak"] is not None else None

    def last_equity(self, module="crypto"):
        """Último equity registrado (no el pico histórico) PARA ESE MÓDULO.
        Es el que hay que usar como base para aplicar el P&L de un trade
        que se acaba de cerrar en ese mismo módulo."""
        row = self.conn.execute(
            "SELECT equity FROM equity_history WHERE module = ? ORDER BY ts DESC LIMIT 1", (module,)
        ).fetchone()
        return row["equity"] if row and row["equity"] is not None else None

    def apply_binary_signal_pnl(self, module, my_prob, market_price, outcome, exit_price=None):
        """Ver apply_binary_signal_pnl en supabase_db.py (misma lógica, esta
        es la variante SQLite para el modo VPS/local).

        AUDITORÍA (09/09/2026): mismo techo de tamaño (Config.MAX_KELLY_STAKE_PCT)
        que la variante Supabase -- ver comentario ahí y en
        _half_kelly_fraction (weather_signal_engine.py)."""
        base = self.last_equity(module)
        if base is None:
            base = 100.0

        kelly = _half_kelly_fraction(my_prob, market_price, max_pct=Config.MAX_KELLY_STAKE_PCT)
        pnl = 0.0
        if kelly and kelly > 0 and market_price and market_price > 0:
            stake = base * kelly
            if outcome in ("yes", "win"):
                pnl = stake * (1 - market_price) / market_price
            elif outcome in ("no", "loss"):
                pnl = -stake
            elif outcome == "stop" and exit_price is not None:
                pnl = stake * (exit_price - market_price) / market_price

        new_equity = base + pnl
        self.record_equity(new_equity, module=module)
        return new_equity

    def apply_r_multiple_pnl(self, module, r_multiple, risk_pct=1.0):
        """Ver apply_r_multiple_pnl en supabase_db.py (misma lógica, esta es
        la variante SQLite para el modo VPS/local)."""
        base = self.last_equity(module)
        if base is None:
            base = 100.0
        risk_amount = base * (risk_pct / 100.0)
        pnl = risk_amount * r_multiple
        new_equity = base + pnl
        self.record_equity(new_equity, module=module)
        return new_equity

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
    # AUDITORÍA (08/09/2026): setup_type/confidence/score agregados como
    # kwargs opcionales (compatibilidad con callers viejos) para poder
    # desglosar resultados por tipo de setup — ver stats_by_dimension() y
    # analyze_crypto_setups.py.
    def add_open_trade(self, symbol, direction, entry_price, stop_price, target_price, position_size,
                        order_id=None, stop_distance=None, setup_type=None, confidence=None, score=None):
        if stop_distance is None:
            stop_distance = abs(entry_price - stop_price)
        self.conn.execute(
            """INSERT INTO open_trades
            (symbol, direction, entry_price, current_stop, target_price, position_size, order_id, ts_opened,
             stop_distance, setup_type, confidence, score)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (symbol, direction, entry_price, stop_price, target_price, position_size, order_id, time.time(),
             stop_distance, setup_type, confidence, score)
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

    def has_open_trade_for_symbol(self, symbol):
        """
        FIX: paridad con supabase_db.py — ver docstring allá. Sin esto no
        había forma de detectar que un mismo símbolo ya tenía una posición
        abierta antes de abrir otra encima en el siguiente escaneo.
        """
        row = self.conn.execute(
            "SELECT COUNT(*) as n FROM open_trades WHERE symbol = ?", (symbol,)
        ).fetchone()
        return bool(row and row["n"] > 0)

    def update_trade_stop(self, trade_id, new_stop_price, new_order_id=None):
        # NUEVO: `new_order_id` opcional — antes esto solo actualizaba el
        # precio del stop, nunca el order_id de la orden real que lo
        # representa en el exchange. Cuando position_manager.py cancelaba
        # la orden vieja y creaba una nueva (trailing stop), ese nuevo id
        # nunca quedaba guardado — el próximo trailing intentaba cancelar
        # con el id VIEJO (ya inexistente), fallaba en silencio, y se podían
        # ir acumulando órdenes de stop huérfanas en el exchange.
        if new_order_id is not None:
            self.conn.execute(
                "UPDATE open_trades SET current_stop = ?, order_id = ? WHERE id = ?",
                (new_stop_price, new_order_id, trade_id)
            )
        else:
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

        # AUDITORÍA (08/09/2026): setup_type/confidence/score copiados desde
        # open_trades -- trade viene de get_open_trades() (SELECT *), así
        # que ya vienen ahí si la columna existe. sqlite3.Row no tiene
        # .get(), por eso el try/except en vez de trade.get(...).
        try:
            setup_type, confidence, score = trade["setup_type"], trade["confidence"], trade["score"]
        except (IndexError, KeyError):
            setup_type, confidence, score = None, None, None

        self.conn.execute(
            """INSERT INTO closed_trades
            (symbol, direction, entry_price, exit_price, outcome, r_multiple, ts_opened, ts_closed,
             setup_type, confidence, score)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (trade["symbol"], direction, entry, exit_price, outcome, r_multiple, trade["ts_opened"], time.time(),
             setup_type, confidence, score)
        )
        self.conn.execute("DELETE FROM open_trades WHERE id = ?", (trade["id"],))
        self.conn.commit()

        # NUEVO: aplicar el P&L realizado al equity simulado. Sin esto, el
        # equity de modo papel se quedaba pegado en el pico histórico sin
        # importar el resultado de los trades cerrados (ver auditoría).
        # AUDITORÍA (07/09/2026): base bajada de 10000.0 a 100.0 -- ver mismo
        # cambio en supabase_db.py.
        position_size = trade.get("position_size")
        if position_size:
            sign = 1 if direction == "LONG" else -1
            pnl_dollars = (exit_price - entry) * position_size * sign
            base_equity = self.last_equity("crypto")
            if base_equity is None:
                base_equity = 100.0
            self.record_equity(base_equity + pnl_dollars, module="crypto")

        return r_multiple

    def get_closed_trades(self, limit=500):
        return self.conn.execute(
            "SELECT * FROM closed_trades ORDER BY ts_closed DESC LIMIT ?", (limit,)
        ).fetchall()

    def stats_by_symbol(self, since_ts=None):
        """Win rate/expectancy por símbolo, opcionalmente desde una fecha (epoch)."""
        return self.stats_by_dimension("symbol", since_ts=since_ts)

    # AUDITORÍA (08/09/2026): generalización de stats_by_symbol para poder
    # desglosar también por setup_type y confidence (ver analyze_crypto_setups.py)
    # sin duplicar la misma lógica de agrupación tres veces. `field` debe ser
    # una columna real de closed_trades -- no viene de input de usuario, así
    # que no hay riesgo de inyección al interpolarla en el SELECT.
    #
    # FIX (08/09/2026, mismo criterio que el fix paralelo en
    # dashboard/app/api/data/route.js/computeStats): ganador/perdedor se
    # define por el SIGNO de r_multiple, no por el motivo de cierre
    # (outcome). Un trade que sale por trailing stop pero cierra en verde
    # (outcome="stop" con r_multiple>0) es una victoria real -- contarlo
    # como derrota subestima win_rate y profit_factor.
    def stats_by_dimension(self, field, since_ts=None):
        query = f"SELECT {field} AS grp, outcome, r_multiple FROM closed_trades WHERE r_multiple IS NOT NULL"
        params = ()
        if since_ts is not None:
            query += " AND ts_closed >= ?"
            params = (since_ts,)
        rows = self.conn.execute(query, params).fetchall()
        by_group = {}
        for r in rows:
            key = r["grp"] if r["grp"] is not None else "(sin dato)"
            by_group.setdefault(key, []).append(r)
        result = {}
        for key, trades in by_group.items():
            n = len(trades)
            wins = [t["r_multiple"] for t in trades if t["r_multiple"] > 0]
            gross_win = sum(t["r_multiple"] for t in trades if t["r_multiple"] > 0)
            gross_loss = abs(sum(t["r_multiple"] for t in trades if t["r_multiple"] < 0))
            result[key] = {
                "n": n,
                "win_rate": len(wins) / n * 100,
                "expectancy_r": sum(t["r_multiple"] for t in trades) / n,
                "total_r": sum(t["r_multiple"] for t in trades),
                "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
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
    def record_polymarket_signal(self, condition_id, question, direction, token_id, entry, target, stop, score=None, confidence=None):
        self.conn.execute(
            """INSERT INTO polymarket_signals
            (condition_id, question, direction, token_id, entry, target, stop, ts_signaled, score, confidence)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (condition_id, question, direction, token_id, entry, target, stop, time.time(), score, confidence)
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
        """
        Win rate, expectancy y profit factor reales sobre trades ya cerrados.

        FIX (08/09/2026, mismo criterio que dashboard/app/api/data/route.js
        y stats_by_dimension() de acá arriba): ganador se define por el
        SIGNO de r_multiple, no por outcome=="target" -- un trade que sale
        por trailing stop en verde (outcome="stop", r_multiple>0) es una
        victoria real.
        """
        query = "SELECT outcome, r_multiple FROM closed_trades WHERE r_multiple IS NOT NULL"
        params = ()
        if since_ts is not None:
            query += " AND ts_closed >= ?"
            params = (since_ts,)
        rows = self.conn.execute(query, params).fetchall()
        n = len(rows)
        if n == 0:
            return {"n": 0, "win_rate": None, "expectancy_r": None, "profit_factor": None}
        wins = [r["r_multiple"] for r in rows if r["r_multiple"] > 0]
        losses = [r["r_multiple"] for r in rows if r["r_multiple"] < 0]
        gross_win = sum(wins)
        gross_loss = abs(sum(losses))
        return {
            "n": n,
            "win_rate": len(wins) / n * 100,
            "expectancy_r": sum(r["r_multiple"] for r in rows) / n,
            "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
        }

    # NUEVO: Tracking de resultados de señales de clima (calibración)
    # NUEVO (08/09/2026): target_date/trajectory_slope_f_per_hr, mismo
    # agregado que en supabase_db.py -- ver comentario ahí para el porqué
    # (investigar el hallazgo de calibración horaria pendiente).
    def record_weather_signal(self, condition_id, question, event_title, station_icao,
                               my_prob, market_price, ev, center_estimate_f, sigma, yes_token_id,
                               target_date=None, trajectory_slope_f_per_hr=None):
        self.conn.execute(
            """INSERT INTO weather_signals
            (condition_id, question, event_title, station_icao, my_prob, market_price,
             ev, center_estimate_f, sigma, yes_token_id, target_date, trajectory_slope_f_per_hr, ts_signaled)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (condition_id, question, event_title, station_icao, my_prob, market_price,
             ev, center_estimate_f, sigma, yes_token_id, target_date, trajectory_slope_f_per_hr, time.time())
        )
        self.conn.commit()

    def get_open_weather_signals(self):
        return self.conn.execute(
            "SELECT * FROM weather_signals WHERE outcome IS NULL"
        ).fetchall()

    # NUEVO (08/09/2026): actual_high_f -- máximo real observado el día
    # que liquida el mercado, ver comentario en supabase_db.py.
    def resolve_weather_signal(self, signal_id, outcome, actual_high_f=None):
        self.conn.execute(
            "UPDATE weather_signals SET outcome=?, actual_high_f=?, ts_resolved=? WHERE id=?",
            (outcome, actual_high_f, time.time(), signal_id)
        )
        self.conn.commit()

    def weather_calibration_summary(self, bucket_size=0.1):
        """
        Brier score y calibración por rango de probabilidad predicha sobre
        las señales de clima ya resueltas — responde la pregunta que antes
        no se podía responder: "cuando el modelo dice 65%, ¿de verdad
        acierta cerca del 65% de las veces?". Un Brier score de 0 es
        predicción perfecta; 0.25 es equivalente a tirar una moneda siempre
        con 50%; más de 0.25 es peor que no tener modelo.
        """
        rows = self.conn.execute(
            "SELECT my_prob, outcome FROM weather_signals WHERE outcome IS NOT NULL"
        ).fetchall()
        n = len(rows)
        if n == 0:
            return {"n": 0, "brier_score": None, "buckets": []}

        buckets = {}
        brier_sum = 0.0
        for r in rows:
            actual = 1.0 if r["outcome"] == "yes" else 0.0
            brier_sum += (r["my_prob"] - actual) ** 2
            key = min(int(r["my_prob"] / bucket_size), int(1 / bucket_size) - 1)
            b = buckets.setdefault(key, {"predicted": [], "actual": []})
            b["predicted"].append(r["my_prob"])
            b["actual"].append(actual)

        bucket_rows = []
        for key in sorted(buckets):
            b = buckets[key]
            bucket_rows.append({
                "range": f"{key*bucket_size*100:.0f}-{(key+1)*bucket_size*100:.0f}%",
                "n": len(b["predicted"]),
                "avg_predicted": sum(b["predicted"]) / len(b["predicted"]),
                "actual_freq": sum(b["actual"]) / len(b["actual"]),
            })

        return {"n": n, "brier_score": brier_sum / n, "buckets": bucket_rows}
