"""
Persistencia en Supabase (Postgres vía su API REST), para el modo serverless
en Vercel. Misma interfaz que db.py (SQLite, modo VPS).
"""
import json
import time
from datetime import datetime, timezone, timedelta
from supabase import create_client

def _now_iso():
    """Timestamps ISO 8601 nativos de PostgreSQL (timestamptz)."""
    return datetime.now(timezone.utc).isoformat()

class SupabaseDatabase:
    def __init__(self, url, key):
        self.client = create_client(url, key)

    def record_equity(self, equity):
        self.client.table("equity_history").insert({"ts": _now_iso(), "equity": equity}).execute()

    def peak_equity(self):
        res = self.client.table("equity_history").select("equity").order("equity", desc=True).limit(1).execute()
        return res.data[0]["equity"] if res.data else None

    def current_exposure_pct(self, equity):
        cutoff = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        res = self.client.table("decisions").select("plan_detail").in_("decision", ["approved", "auto_executed"]).gte("ts", cutoff).execute()
        total_risk = sum((row.get("plan_detail") or {}).get("risk_amount", 0.0) for row in res.data or [])
        return (total_risk / equity) * 100 if equity > 0 else 0.0

    def get_state(self, key, default=None):
        res = self.client.table("bot_state").select("value").eq("key", key).execute()
        return res.data[0]["value"] if res.data else default

    def set_state(self, key, value):
        self.client.table("bot_state").upsert({"key": key, "value": str(value)}).execute()

    def log_decision(self, symbol, signal, risk_report, plan, decision, order_detail=None):
        self.client.table("decisions").insert({
            "ts": _now_iso(), "symbol": symbol,
            "signal_type": signal.get("type") if signal else None,
            "direction": signal.get("direction") if signal else None,
            "confidence": signal.get("confidence") if signal else None,
            "risk_pass": bool(risk_report["pass"]) if risk_report else None,
            "risk_detail": risk_report, "plan_detail": plan, "decision": decision, "order_detail": order_detail,
        }).execute()

    def recent_decisions(self, limit=20):
        return self.client.table("decisions").select("*").order("ts", desc=True).limit(limit).execute().data

    def record_indicator_snapshot(self, symbol, snapshot):
        self.client.table("indicator_snapshots").insert({
            "symbol": symbol, "ts": _now_iso(), "price": snapshot.get("price"), "rsi": snapshot.get("rsi"),
            "atr_pct": snapshot.get("atr_pct"), "volume_ratio": snapshot.get("volume_ratio"),
            "volatility": snapshot.get("volatility"), "momentum": snapshot.get("momentum"),
            "trend_align": snapshot.get("trend_align"), "trend_bias": snapshot.get("trend_bias"),
        }).execute()

    def count_open_trades_by_direction(self, direction):
        res = self.client.table("open_trades").select("id", count="exact").eq("direction", direction).execute()
        return res.count or 0

    def get_open_trades(self):
        return self.client.table("open_trades").select("*").execute().data or []

    def has_open_trade_for_symbol(self, symbol):
        res = self.client.table("open_trades").select("id", count="exact").eq("symbol", symbol).limit(1).execute()
        return (res.count or 0) > 0

    def add_open_trade(self, symbol, direction, entry_price, stop_price, target_price, position_size, order_id=None, stop_distance=None):
        if stop_distance is None:
            stop_distance = abs(entry_price - stop_price)
        self.client.table("open_trades").insert({
            "symbol": symbol, "direction": direction, "entry_price": entry_price, "current_stop": stop_price,
            "target_price": target_price, "position_size": position_size, "order_id": order_id,
            "ts_opened": _now_iso(), "stop_distance": stop_distance,
        }).execute()

    def close_trade_with_outcome(self, trade, exit_price, outcome):
        deleted = self.client.table("open_trades").delete().eq("id", trade["id"]).execute()
        if not deleted.data:
            return False
        entry, direction, stop_distance = trade["entry_price"], trade["direction"], trade.get("stop_distance")
        r_multiple = ((exit_price - entry) / stop_distance) * (1 if direction == "LONG" else -1) if stop_distance else None
        self.client.table("closed_trades").insert({
            "symbol": trade["symbol"], "direction": direction, "entry_price": entry, "exit_price": exit_price,
            "outcome": outcome, "r_multiple": r_multiple, "ts_opened": trade["ts_opened"], "ts_closed": _now_iso(),
        }).execute()
        return r_multiple

    def stats_summary(self, since_ts=None):
        query = self.client.table("closed_trades").select("outcome,r_multiple")
        if since_ts is not None:
            query = query.gte("ts_closed", since_ts)
        rows = [r for r in query.execute().data or [] if r.get("r_multiple") is not None]
        n = len(rows)
        if n == 0:
            return {"n": 0, "win_rate": None, "expectancy_r": None, "profit_factor": None}
        wins = [r["r_multiple"] for r in rows if r["outcome"] == "target"]
        losses = [r["r_multiple"] for r in rows if r["outcome"] == "stop"]
        gross_win, gross_loss = sum(r for r in wins if r > 0), abs(sum(r for r in losses if r < 0))
        return {"n": n, "win_rate": len(wins) / n * 100, "expectancy_r": sum(r["r_multiple"] for r in rows) / n, "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None}

    def record_polymarket_signal(self, condition_id, question, direction, token_id, entry, target, stop):
        self.client.table("polymarket_signals").insert({"condition_id": condition_id, "question": question, "direction": direction, "token_id": token_id, "entry": entry, "target": target, "stop": stop, "ts_signaled": _now_iso()}).execute()

    def get_open_polymarket_signals(self):
        return self.client.table("polymarket_signals").select("*").is_("outcome", "null").execute().data or []

    def resolve_polymarket_signal(self, signal_id, exit_price, outcome):
        res = self.client.table("polymarket_signals").update({"outcome": outcome, "exit_price": exit_price, "ts_resolved": _now_iso()}).eq("id", signal_id).is_("outcome", "null").execute()
        return bool(res.data)

    def polymarket_stats_summary(self):
        rows = self.client.table("polymarket_signals").select("direction,entry,target,stop,outcome,exit_price").not_.is_("outcome", "null").execute().data or []
        n = len(rows)
        if n == 0:
            return {"n": 0, "win_rate": None, "expectancy_r": None}
        r_multiples, wins = [], 0
        for r in rows:
            stop_distance = abs(r["entry"] - r["stop"])
            if stop_distance <= 0: continue
            rm = (r["exit_price"] - r["entry"]) / stop_distance
            r_multiples.append(rm)
            if r["outcome"] == "target": wins += 1
        return {"n": n, "win_rate": wins / n * 100, "expectancy_r": sum(r_multiples) / len(r_multiples) if r_multiples else None}

    def record_weather_signal(self, condition_id, question, event_title, station_icao, my_prob, market_price, ev, center_estimate_f, sigma, yes_token_id):
        self.client.table("weather_signals").insert({"condition_id": condition_id, "question": question, "event_title": event_title, "station_icao": station_icao, "my_prob": my_prob, "market_price": market_price, "ev": ev, "center_estimate_f": center_estimate_f, "sigma": sigma, "yes_token_id": yes_token_id, "ts_signaled": _now_iso()}).execute()

    def get_open_weather_signals(self):
        return self.client.table("weather_signals").select("*").is_("outcome", "null").execute().data or []

    def resolve_weather_signal(self, signal_id, outcome):
        res = self.client.table("weather_signals").update({"outcome": outcome, "ts_resolved": _now_iso()}).eq("id", signal_id).is_("outcome", "null").execute()
        return bool(res.data)

    def weather_calibration_summary(self, bucket_size=0.1):
        rows = self.client.table("weather_signals").select("my_prob,outcome").not_.is_("outcome", "null").execute().data or []
        n = len(rows)
        if n == 0: return {"n": 0, "brier_score": None, "buckets": []}
        buckets, brier_sum = {}, 0.0
        for r in rows:
            actual = 1.0 if r["outcome"] == "yes" else 0.0
            brier_sum += (r["my_prob"] - actual) ** 2
            key = min(int(r["my_prob"] / bucket_size), int(1 / bucket_size) - 1)
            b = buckets.setdefault(key, {"predicted": [], "actual": []})
            b["predicted"].append(r["my_prob"]); b["actual"].append(actual)
        bucket_rows = [{"range": f"{k*bucket_size*100:.0f}-{(k+1)*bucket_size*100:.0f}%", "n": len(b["predicted"]), "avg_predicted": sum(b["predicted"])/len(b["predicted"]), "actual_freq": sum(b["actual"])/len(b["actual"])} for k, b in sorted(buckets.items())]
        return {"n": n, "brier_score": brier_sum / n, "buckets": bucket_rows}

    # =========================================================================
    # SEMANA 3.5: FIX CRÍTICO ANTI-DUPLICADOS EN POLYMARKET
    # =========================================================================
    
    def has_recent_polymarket_signal(self, condition_id, direction, hours=6.0):
        """
        Fuente de verdad absoluta: revisa la tabla directamente para evitar
        condiciones de carrera (race conditions) entre múltiples instancias de Vercel.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        res = self.client.table("polymarket_signals").select("id").eq("condition_id", condition_id).eq("direction", direction).is_("outcome", "null").gte("ts_signaled", cutoff).limit(1).execute()
        return bool(res.data)

    def should_notify_polymarket(self, condition_id, direction, score, resend_cooldown_hours=6.0, min_score_increase_pct=0.20):
        # 1. Bloqueo primario: ¿Ya existe una señal ABIERTA reciente para esta dirección?
        if self.has_recent_polymarket_signal(condition_id, direction, resend_cooldown_hours):
            return False
        
        # 2. Bloqueo anti-flip-flop: ¿Se notificó la dirección OPUESTA recientemente?
        # (Evita que el bot mande YES y luego NO para el mismo mercado en poco tiempo)
        opposite = "YES" if direction == "NO" else "NO"
        if self.has_recent_polymarket_signal(condition_id, opposite, resend_cooldown_hours):
            return False

        # 3. Chequeo secundario de estado en caché (para lógica de score y compatibilidad)
        # FIX: Ahora la clave INCLUYE la dirección para evitar colisiones YES/NO.
        state_key = f"poly_notify_{condition_id}_{direction}"
        prev_str = self.get_state(state_key)
        if prev_str is None:
            return True
        try:
            prev = json.loads(prev_str)
        except (json.JSONDecodeError, TypeError):
            return True
        
        prev_ts = prev.get("ts", 0)
        if isinstance(prev_ts, str):
            prev_ts = datetime.fromisoformat(prev_ts.replace('Z', '+00:00')).timestamp()
        
        if (time.time() - prev_ts) >= resend_cooldown_hours * 3600:
            return True
            
        prev_score = prev.get("score", 0) or 0
        if prev_score > 0 and score >= prev_score * (1 + min_score_increase_pct):
            return True
            
        return False

    def record_notified_polymarket(self, condition_id, direction, score):
        # FIX: La clave ahora incluye la dirección para un aislamiento correcto.
        state_key = f"poly_notify_{condition_id}_{direction}"
        self.set_state(state_key, json.dumps({"ts": _now_iso(), "direction": direction, "score": score}))

    # =========================================================================

    def create_pending_decision(self, message_id, symbol, signal, risk_report, plan):
        self.client.table("pending_decisions").insert({"message_id": message_id, "ts": _now_iso(), "symbol": symbol, "signal": signal, "risk_report": risk_report, "plan": plan, "resolved": False}).execute()

    def get_pending_decision(self, message_id):
        res = self.client.table("pending_decisions").select("*").eq("message_id", message_id).eq("resolved", False).execute()
        return res.data[0] if res.data else None

    def expire_stale_pending_decisions(self, older_than_seconds):
        cutoff = datetime.now(timezone.utc).timestamp() - older_than_seconds
        res = self.client.table("pending_decisions").select("*").eq("resolved", False).execute()
        to_expire = [r["message_id"] for r in (res.data or []) if (datetime.fromisoformat(r["ts"].replace('Z', '+00:00')).timestamp() if isinstance(r["ts"], str) else r["ts"]) < cutoff]
        if to_expire:
            self.client.table("pending_decisions").update({"resolved": True}).in_("message_id", to_expire).execute()
            return [r for r in (res.data or []) if r["message_id"] in to_expire]
        return []

    def claim_pending_decision(self, message_id):
        res = self.client.table("pending_decisions").update({"resolved": True}).eq("message_id", message_id).eq("resolved", False).execute()
        return res.data[0] if res.data else None

    def has_open_pending_decision(self):
        return bool(self.client.table("pending_decisions").select("message_id").eq("resolved", False).limit(1).execute().data)

    def resolve_pending_decision(self, message_id):
        self.client.table("pending_decisions").update({"resolved": True}).eq("message_id", message_id).execute()
