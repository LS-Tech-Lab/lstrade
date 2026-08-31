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
        """
        Devuelve el r_multiple (float o None) si este llamado cerró el
        trade de verdad, o False si otra invocación ya se lo había llevado.

        El DELETE va primero, a propósito: es la operación atómica que
        decide quién "gana" si dos invocaciones de /api/manage_positions
        se solapan (cron atrasado + el siguiente disparo, o un reintento) —
        Postgres solo deja que una transacción borre esa fila. Antes se
        insertaba en closed_trades primero y se borraba después; si dos
        invocaciones pasaban ambas el chequeo antes de que cualquiera
        borrara, el mismo trade quedaba duplicado en closed_trades para
        siempre, ensuciando stats_summary() (win rate, expectancy) sin
        forma de detectarlo después.
        """
        deleted = self.client.table("open_trades").delete().eq("id", trade["id"]).execute()
        if not deleted.data:
            return False

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

    def get_open_polymarket_signals(self):
        """
        Faltaba en Supabase — sin esto, api/polymarket_track_results.py no
        tenía forma de saber qué señales de Polymarket seguían pendientes
        de resultado (existía la escritura vía record_polymarket_signal,
        pero nunca la lectura del lado "abierto").
        """
        res = self.client.table("polymarket_signals").select("*").is_("outcome", "null").execute()
        return res.data or []

    def resolve_polymarket_signal(self, signal_id, exit_price, outcome):
        """
        Devuelve True si esta llamada resolvió la señal, False si otra
        invocación (cron solapado) ya lo había hecho — mismo patrón que
        close_trade_with_outcome: el WHERE outcome IS NULL hace que la
        operación sea atómica, así el caller sabe si mandar el aviso de
        Telegram o no. Antes esto no devolvía nada, así que dos
        invocaciones concurrentes podían mandar el mismo aviso dos veces.
        """
        res = self.client.table("polymarket_signals").update({
            "outcome": outcome, "exit_price": exit_price, "ts_resolved": time.time(),
        }).eq("id", signal_id).is_("outcome", "null").execute()
        return bool(res.data)

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

    # --- señales de clima (calibración) ---
    def record_weather_signal(self, condition_id, question, event_title, station_icao,
                               my_prob, market_price, ev, center_estimate_f, sigma, yes_token_id):
        self.client.table("weather_signals").insert({
            "condition_id": condition_id, "question": question, "event_title": event_title,
            "station_icao": station_icao, "my_prob": my_prob, "market_price": market_price,
            "ev": ev, "center_estimate_f": center_estimate_f, "sigma": sigma,
            "yes_token_id": yes_token_id, "ts_signaled": time.time(),
        }).execute()

    def get_open_weather_signals(self):
        res = self.client.table("weather_signals").select("*").is_("outcome", "null").execute()
        return res.data or []

    def resolve_weather_signal(self, signal_id, outcome):
        """Ver docstring de resolve_polymarket_signal — mismo patrón atómico."""
        res = self.client.table("weather_signals").update({
            "outcome": outcome, "ts_resolved": time.time(),
        }).eq("id", signal_id).is_("outcome", "null").execute()
        return bool(res.data)

    def weather_calibration_summary(self, bucket_size=0.1):
        """Ver docstring de Database.weather_calibration_summary (db.py) —
        misma lógica, contra Supabase en vez de SQLite."""
        res = self.client.table("weather_signals").select("my_prob,outcome") \
            .not_.is_("outcome", "null").execute()
        rows = res.data or []
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

    def expire_stale_pending_decisions(self, older_than_seconds):
        """
        Vence (resolved=True, sin ejecutar nada) las decisiones pendientes
        más viejas que `older_than_seconds` — para que un aviso de Telegram
        que se te pasó no deje el bot mudo indefinidamente (ver el comentario
        largo en config.py, PENDING_DECISION_EXPIRY_SECONDS). Devuelve la
        lista de las que venció, para poder avisar y loguearlas como
        'expired' en vez de que desaparezcan sin dejar rastro.
        """
        cutoff = time.time() - older_than_seconds
        res = (
            self.client.table("pending_decisions")
            .update({"resolved": True})
            .eq("resolved", False)
            .lt("ts", cutoff)
            .execute()
        )
        return res.data or []

    def claim_pending_decision(self, message_id):
        """
        Lee Y marca como resuelta la decisión en una sola operación atómica
        (UPDATE ... WHERE resolved=false, Postgres solo deja que una sola
        transacción concurrente gane esa fila). Reemplaza al patrón
        get_pending_decision() + ejecutar orden + resolve_pending_decision()
        de 3 pasos separados: si Telegram reentrega el webhook (pasa si la
        respuesta tarda) o el usuario toca "Aprobar" dos veces, dos
        invocaciones concurrentes podían leer la misma decisión como
        pendiente antes de que ninguna la marcara resuelta, y las dos
        ejecutaban la orden — con LIVE_TRADING=true eso es plata real
        duplicada, no solo un dato mal contado.
        """
        res = (
            self.client.table("pending_decisions")
            .update({"resolved": True})
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
