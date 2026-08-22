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
