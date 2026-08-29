"""
Persistencia en Supabase (Postgres vía su API REST), para el modo serverless
en Vercel. Misma interfaz que db.py (SQLite, modo VPS) más el manejo de
decisiones pendientes de aprobación por Telegram.

Requiere las variables de entorno SUPABASE_URL y SUPABASE_KEY (usá la
service_role key, no la anon key — esto corre del lado del servidor).
Correr antes schema.sql en el SQL Editor de tu proyecto de Supabase.
"""
import time
from supabase import create_client


class SupabaseDatabase:
    def __init__(self, url, key):
        self.client = create_client(url, key)

    # --- equity / drawdown ---
    def record_equity(self, equity):
        self.client.table("equity_history").insert({"ts": time.time(), "equity": equity}).execute()

    def peak_equity(self):
        res = (
            self.client.table("equity_history")
            .select("equity")
            .order("equity", desc=True)
            .limit(1)
            .execute()
        )
        return res.data[0]["equity"] if res.data else None

    def current_exposure_pct(self, equity):
        cutoff = time.time() - 24 * 3600
        res = (
            self.client.table("decisions")
            .select("plan_detail")
            .in_("decision", ["approved", "auto_executed"])
            .gt("ts", cutoff)
            .execute()
        )
        total_risk = 0.0
        for row in res.data or []:
            plan = row.get("plan_detail") or {}
            total_risk += plan.get("risk_amount", 0.0)
        if equity <= 0:
            return 0.0
        return (total_risk / equity) * 100

    # --- estado (circuit breaker, halted, etc) ---
    def get_state(self, key, default=None):
        res = self.client.table("bot_state").select("value").eq("key", key).execute()
        return res.data[0]["value"] if res.data else default

    def set_state(self, key, value):
        self.client.table("bot_state").upsert({"key": key, "value": str(value)}).execute()

    # --- bitácora ---
    def log_decision(self, symbol, signal, risk_report, plan, decision, order_detail=None):
        self.client.table("decisions").insert({
            "ts": time.time(),
            "symbol": symbol,
            "signal_type": signal.get("type") if signal else None,
            "direction": signal.get("direction") if signal else None,
            "confidence": signal.get("confidence") if signal else None,
            "risk_pass": bool(risk_report["pass"]) if risk_report else None,
            "risk_detail": risk_report,
            "plan_detail": plan,
            "decision": decision,
            "order_detail": order_detail,
        }).execute()

    def recent_decisions(self, limit=20):
        res = (
            self.client.table("decisions")
            .select("*")
            .order("ts", desc=True)
            .limit(limit)
            .execute()
        )
        return res.data

    # --- posiciones abiertas / correlación (paridad con db.py) ---
    def count_open_trades_by_direction(self, direction):
        """
        Requerido por RiskManager.check() (exposición correlacionada) — sin
        esto, api/cycle.py rompía al llamar risk_manager.check() en modo
        serverless, porque este método no existía acá.
        """
        res = (
            self.client.table("open_trades")
            .select("id", count="exact")
            .eq("direction", direction)
            .execute()
        )
        return res.count or 0

    def get_open_trades(self):
        res = self.client.table("open_trades").select("*").execute()
        return res.data or []

    def add_open_trade(self, symbol, direction, entry_price, stop_price, target_price, position_size,
                        order_id=None, stop_distance=None):
        if stop_distance is None:
            stop_distance = abs(entry_price - stop_price)
        self.client.table("open_trades").insert({
            "symbol": symbol, "direction": direction, "entry_price": entry_price,
            "current_stop": stop_price, "target_price": target_price,
            "position_size": position_size, "order_id": order_id,
            "ts_opened": time.time(), "stop_distance": stop_distance,
        }).execute()

    def close_trade_with_outcome(self, trade, exit_price, outcome):
        entry = trade["entry_price"]
        direction = trade["direction"]
        stop_distance = trade.get("stop_distance")
        r_multiple = None
        if stop_distance:
            sign = 1 if direction == "LONG" else -1
            r_multiple = ((exit_price - entry) / stop_distance) * sign
        self.client.table("closed_trades").insert({
            "symbol": trade["symbol"], "direction": direction, "entry_price": entry,
            "exit_price": exit_price, "outcome": outcome, "r_multiple": r_multiple,
            "ts_opened": trade["ts_opened"], "ts_closed": time.time(),
        }).execute()
        self.client.table("open_trades").delete().eq("id", trade["id"]).execute()
        return r_multiple

    def stats_summary(self, since_ts=None):
        query = self.client.table("closed_trades").select("outcome,r_multiple")
        if since_ts is not None:
            query = query.gte("ts_closed", since_ts)
        res = query.execute()
        rows = [r for r in (res.data or []) if r.get("r_multiple") is not None]
        n = len(rows)
        if n == 0:
            return {"n": 0, "win_rate": None, "expectancy_r": None, "profit_factor": None}
        wins = [r["r_multiple"] for r in rows if r["outcome"] == "target"]
        losses = [r["r_multiple"] for r in rows if r["outcome"] == "stop"]
        gross_win = sum(r for r in wins if r > 0)
        gross_loss = abs(sum(r for r in losses if r < 0))
        return {
            "n": n, "win_rate": len(wins) / n * 100,
            "expectancy_r": sum(r["r_multiple"] for r in rows) / n,
            "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
        }

    # --- señales de Polymarket (paridad con db.py) ---
    def record_polymarket_signal(self, condition_id, question, direction, token_id, entry, target, stop):
        self.client.table("polymarket_signals").insert({
            "condition_id": condition_id, "question": question, "direction": direction,
            "token_id": token_id, "entry": entry, "target": target, "stop": stop,
            "ts_signaled": time.time(),
        }).execute()

    def polymarket_stats_summary(self):
        res = self.client.table("polymarket_signals").select("direction,entry,target,stop,outcome,exit_price") \
            .not_.is_("outcome", "null").execute()
        rows = res.data or []
        n = len(rows)
        if n == 0:
            return {"n": 0, "win_rate": None, "expectancy_r": None}
        r_multiples, wins = [], 0
        for r in rows:
            stop_distance = abs(r["entry"] - r["stop"])
            if stop_distance <= 0:
                continue
            rm = (r["exit_price"] - r["entry"]) / stop_distance
            r_multiples.append(rm)
            if r["outcome"] == "target":
                wins += 1
        return {
            "n": n, "win_rate": wins / n * 100,
            "expectancy_r": sum(r_multiples) / len(r_multiples) if r_multiples else None,
        }

    # NUEVO: snapshot de indicadores por símbolo, independiente de si hubo
    # señal de trading — ver compute_indicator_snapshot() en signal_engine.py.
    def record_indicator_snapshot(self, symbol, snapshot):
        self.client.table("indicator_snapshots").insert({
            "symbol": symbol, "ts": time.time(),
            "price": snapshot.get("price"), "rsi": snapshot.get("rsi"),
            "atr_pct": snapshot.get("atr_pct"), "volume_ratio": snapshot.get("volume_ratio"),
            "volatility": snapshot.get("volatility"), "momentum": snapshot.get("momentum"),
            "trend_align": snapshot.get("trend_align"), "trend_bias": snapshot.get("trend_bias"),
        }).execute()

    def latest_indicator_snapshots(self):
        """
        Un snapshot por símbolo (el más reciente) — se trae ordenado por ts
        desc con un límite generoso y se deduplica en Python en vez de un
        DISTINCT ON de Postgres, porque supabase-py no expone esa cláusula
        directamente sobre el query builder.
        """
        res = (
            self.client.table("indicator_snapshots")
            .select("*")
            .order("ts", desc=True)
            .limit(200)
            .execute()
        )
        latest_by_symbol = {}
        for row in res.data or []:
            if row["symbol"] not in latest_by_symbol:
                latest_by_symbol[row["symbol"]] = row
        return list(latest_by_symbol.values())

    # --- tracking de resultados de Polymarket (paridad con db.py) ---
    # Sin esto, polymarket_track_results.py / api/polymarket_resolve.py no
    # pueden resolver señales en modo serverless: check_open_signals() llama
    # a estos dos métodos sobre lo que sea que se le pase como `db`.
    def get_open_polymarket_signals(self):
        res = self.client.table("polymarket_signals").select("*").is_("outcome", "null").execute()
        return res.data or []

    def resolve_polymarket_signal(self, signal_id, exit_price, outcome):
        self.client.table("polymarket_signals").update({
            "outcome": outcome, "exit_price": exit_price, "ts_resolved": time.time(),
        }).eq("id", signal_id).execute()

    def polymarket_stats_by_category(self):
        """Mismo desglose que db.py — ver ahí para el detalle del razonamiento."""
        from polymarket_categories import categorize

        res = self.client.table("polymarket_signals") \
            .select("question,entry,target,stop,outcome,exit_price") \
            .not_.is_("outcome", "null").execute()
        by_category = {}
        for r in res.data or []:
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
                "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
                "total_r": sum(rm for rm, _ in entries),
            }
        return result

    # --- dedup de avisos de Polymarket entre invocaciones serverless ---
    # Reemplaza a PolymarketStateStore (JSON en disco), que no persiste
    # entre invocaciones de una función serverless — cada una arranca con
    # filesystem limpio, así que sin esto se reenviaría la misma señal en
    # cada ciclo de 10 min mientras el mercado siga pasando el umbral.
    def should_notify_polymarket(self, condition_id, direction, score,
                                  resend_cooldown_hours=6.0, min_score_increase_pct=0.20):
        res = self.client.table("polymarket_notify_state").select("*") \
            .eq("condition_id", condition_id).execute()
        if not res.data:
            return True
        prev = res.data[0]
        elapsed = time.time() - prev["ts"]
        if elapsed >= resend_cooldown_hours * 3600:
            return True
        if prev["direction"] != direction:
            return True
        if prev["score"] > 0 and score >= prev["score"] * (1 + min_score_increase_pct):
            return True
        return False

    def record_notified_polymarket(self, condition_id, direction, score):
        self.client.table("polymarket_notify_state").upsert({
            "condition_id": condition_id, "direction": direction,
            "score": score, "ts": time.time(),
        }).execute()

    # --- decisiones pendientes de aprobación por Telegram (solo modo serverless) ---
    def create_pending_decision(self, message_id, symbol, signal, risk_report, plan):
        self.client.table("pending_decisions").insert({
            "message_id": message_id,
            "ts": time.time(),
            "symbol": symbol,
            "signal": signal,
            "risk_report": risk_report,
            "plan": plan,
            "resolved": False,
        }).execute()

    def get_pending_decision(self, message_id):
        res = (
            self.client.table("pending_decisions")
            .select("*")
            .eq("message_id", message_id)
            .eq("resolved", False)
            .execute()
        )
        return res.data[0] if res.data else None

    def has_open_pending_decision(self):
        res = self.client.table("pending_decisions").select("message_id").eq("resolved", False).limit(1).execute()
        return bool(res.data)

    def resolve_pending_decision(self, message_id):
        self.client.table("pending_decisions").update({"resolved": True}).eq("message_id", message_id).execute()
