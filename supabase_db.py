"""
Persistencia en Supabase (Postgres vía su API REST), para el modo serverless
en Vercel. Misma interfaz que db.py (SQLite, modo VPS).
"""
import json
import time
from datetime import datetime, timezone, timedelta
from supabase import create_client

from weather_signal_engine import _half_kelly_fraction

def _now_iso():
    """Timestamps ISO 8601 nativos de PostgreSQL (timestamptz)."""
    return datetime.now(timezone.utc).isoformat()

class SupabaseDatabase:
    def __init__(self, url, key):
        self.client = create_client(url, key)

    def record_equity(self, equity, module="crypto"):
        # AUDITORÍA (07/09/2026, pedido del usuario): se agrega `module` --
        # antes equity_history era una sola serie global que en la práctica
        # solo actualizaba cripto (close_trade_with_outcome más abajo). El
        # usuario pidió una serie de equity POR MÓDULO (cripto/clima/
        # Polymarket/MLB), cada una arrancando en $100 -- ver migración
        # add_module_to_equity_history (agrega la columna + rescala el
        # historial de cripto de base 10000 a base 100) y los métodos
        # apply_binary_signal_pnl/apply_r_multiple_pnl más abajo, que son
        # los que alimentan las series de clima/Polymarket/MLB.
        self.client.table("equity_history").insert({"ts": _now_iso(), "equity": equity, "module": module}).execute()

    def peak_equity(self, module="crypto"):
        res = self.client.table("equity_history").select("equity").eq("module", module).order("equity", desc=True).limit(1).execute()
        return res.data[0]["equity"] if res.data else None

    def last_equity(self, module="crypto"):
        """Último equity registrado (no el pico histórico) PARA ESE MÓDULO.
        Es el que hay que usar como base para aplicar el P&L de un trade
        que se acaba de cerrar en ese mismo módulo."""
        res = self.client.table("equity_history").select("equity").eq("module", module).order("ts", desc=True).limit(1).execute()
        return res.data[0]["equity"] if res.data else None

    def apply_binary_signal_pnl(self, module, my_prob, market_price, outcome, exit_price=None):
        """
        NUEVO (07/09/2026, pedido del usuario): simula el equity de un
        módulo de mercados binarios (clima, MLB -- ambos con my_prob/
        market_price ya armados sobre el lado comprado) aplicando ½ Kelly
        como tamaño de apuesta, igual que ya se le sugiere informativamente
        al usuario en build_weather_memo()/build_mlb_memo() vía
        _half_kelly_fraction(). Arranca en $100 si el módulo no tiene
        historial todavía (mismo default que la base de cripto post-rescale).

        outcome esperado: "yes"/"win" (ganó el lado comprado), "no"/"loss"
        (perdió), "stop" (salida anticipada a `exit_price`, ver
        WEATHER_MLB_STOP_LOSS_PCT en config.py), "void"/cualquier otro
        (sin P&L real -- partido cancelado o similar, no mueve el equity
        pero tampoco rompe si se llama).

        Devuelve el nuevo equity del módulo.
        """
        base = self.last_equity(module)
        if base is None:
            base = 100.0

        kelly = _half_kelly_fraction(my_prob, market_price)
        pnl = 0.0
        if kelly and kelly > 0 and market_price and market_price > 0:
            stake = base * kelly
            if outcome in ("yes", "win"):
                pnl = stake * (1 - market_price) / market_price
            elif outcome in ("no", "loss"):
                pnl = -stake
            elif outcome == "stop" and exit_price is not None:
                pnl = stake * (exit_price - market_price) / market_price
            # "void" u otro outcome: pnl se queda en 0.0 -- sin apuesta real que resolver.

        new_equity = base + pnl
        self.record_equity(new_equity, module=module)
        return new_equity

    def apply_r_multiple_pnl(self, module, r_multiple, risk_pct=1.0):
        """
        NUEVO (07/09/2026, pedido del usuario): equivalente de
        apply_binary_signal_pnl() para Polymarket genérico, que no arma
        una probabilidad de fundamentos (`my_prob`) sino un plan de
        entrada/target/stop -- ahí ½ Kelly no aplica (no hay `prob` con
        qué calcularlo), así que se reusa el mismo esquema de riesgo fijo
        por operación que ya usa risk_manager.py para cripto
        (RISK_PCT_PER_TRADE, default 1.0% -- ver config.py): se arriesga
        ese % del equity del módulo por señal, y el resultado (r_multiple,
        ya calculado igual que en polymarket_stats_summary/
        polymarket_recent_history) determina la ganancia o pérdida real.
        Arranca en $100 si el módulo no tiene historial todavía.
        """
        base = self.last_equity(module)
        if base is None:
            base = 100.0
        risk_amount = base * (risk_pct / 100.0)
        pnl = risk_amount * r_multiple
        new_equity = base + pnl
        self.record_equity(new_equity, module=module)
        return new_equity

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

    # AUDITORÍA (08/09/2026): setup_type/confidence/score agregados como
    # kwargs opcionales (compatibilidad con callers viejos) para poder
    # desglosar resultados por tipo de setup -- ver stats_by_dimension() y
    # analyze_crypto_setups.py. Se propagan a closed_trades al cerrar (ver
    # close_trade_with_outcome de acá abajo).
    def add_open_trade(self, symbol, direction, entry_price, stop_price, target_price, position_size,
                        order_id=None, stop_distance=None, setup_type=None, confidence=None, score=None):
        if stop_distance is None:
            stop_distance = abs(entry_price - stop_price)
        try:
            self.client.table("open_trades").insert({
                "symbol": symbol, "direction": direction, "entry_price": entry_price, "current_stop": stop_price,
                "target_price": target_price, "position_size": position_size, "order_id": order_id,
                "ts_opened": _now_iso(), "stop_distance": stop_distance,
                "setup_type": setup_type, "confidence": confidence, "score": score,
            }).execute()
            return True
        except Exception as e:
            # FIX (auditoría 02/09/2026): uq_open_trades_symbol (schema.sql)
            # es el refuerzo a nivel DB de has_open_trade_for_symbol() -- ese
            # chequeo es un SELECT-then-INSERT sin lock, así que dos
            # invocaciones casi simultáneas de /api/cycle podrían ambas
            # pasarlo antes de insertar. Si la causa del error es justo la
            # violación de unicidad (código 23505 de Postgres), es el caso
            # esperado de "otra invocación ya abrió esto" -- se traga acá en
            # vez de devolver un 500, porque la posición real ya quedó
            # registrada por la otra invocación. Cualquier otro error de
            # inserción sí se re-lanza.
            code = getattr(e, "code", None) or (e.args[0].get("code") if e.args and isinstance(e.args[0], dict) else None)
            if code == "23505" or "duplicate key" in str(e).lower() or "uq_open_trades_symbol" in str(e).lower():
                return False
            raise

    def update_trade_stop(self, trade_id, new_stop_price, new_order_id=None):
        update = {"current_stop": new_stop_price}
        if new_order_id is not None:
            update["order_id"] = new_order_id
        self.client.table("open_trades").update(update).eq("id", trade_id).execute()

    def close_trade_with_outcome(self, trade, exit_price, outcome):
        deleted = self.client.table("open_trades").delete().eq("id", trade["id"]).execute()
        if not deleted.data:
            return False
        entry, direction, stop_distance = trade["entry_price"], trade["direction"], trade.get("stop_distance")
        r_multiple = ((exit_price - entry) / stop_distance) * (1 if direction == "LONG" else -1) if stop_distance else None
        self.client.table("closed_trades").insert({
            "symbol": trade["symbol"], "direction": direction, "entry_price": entry, "exit_price": exit_price,
            "outcome": outcome, "r_multiple": r_multiple, "ts_opened": trade["ts_opened"], "ts_closed": _now_iso(),
            # AUDITORÍA (08/09/2026): copiados desde open_trades (get_open_trades
            # hace SELECT *, así que ya vienen en `trade` si la columna existe).
            "setup_type": trade.get("setup_type"), "confidence": trade.get("confidence"), "score": trade.get("score"),
        }).execute()

        # NUEVO: aplicar el P&L realizado al equity simulado. Sin esto, el
        # equity de modo papel se quedaba pegado en el pico histórico sin
        # importar el resultado de los trades cerrados (ver auditoría).
        # AUDITORÍA (07/09/2026): base bajada de 10000.0 a 100.0 -- pedido
        # del usuario de arrancar el equity de cripto en $100 (el historial
        # ya existente en Supabase se rescaló x0.01 en la misma migración
        # que baja este default, para que la curva completa siga siendo
        # consistente con la nueva base).
        position_size = trade.get("position_size")
        if position_size:
            sign = 1 if direction == "LONG" else -1
            pnl_dollars = (exit_price - entry) * position_size * sign
            base_equity = self.last_equity("crypto")
            if base_equity is None:
                base_equity = 100.0
            self.record_equity(base_equity + pnl_dollars, module="crypto")

        return r_multiple

    def stats_summary(self, since_ts=None):
        """
        FIX (08/09/2026, mismo criterio que dashboard/app/api/data/route.js
        computeStats() y db.py stats_summary()/stats_by_dimension()):
        ganador se define por el SIGNO de r_multiple, no por
        outcome=="target" -- un trade que sale por trailing stop en verde
        (outcome="stop", r_multiple>0) es una victoria real. Con el
        criterio viejo, contarlo como derrota subestimaba win_rate Y
        profit_factor (quedaba afuera de ambas bolsas).
        """
        query = self.client.table("closed_trades").select("outcome,r_multiple")
        if since_ts is not None:
            query = query.gte("ts_closed", since_ts)
        rows = [r for r in query.execute().data or [] if r.get("r_multiple") is not None]
        n = len(rows)
        if n == 0:
            return {"n": 0, "win_rate": None, "expectancy_r": None, "profit_factor": None}
        wins = [r["r_multiple"] for r in rows if r["r_multiple"] > 0]
        losses = [r["r_multiple"] for r in rows if r["r_multiple"] < 0]
        gross_win, gross_loss = sum(wins), abs(sum(losses))
        return {"n": n, "win_rate": len(wins) / n * 100, "expectancy_r": sum(r["r_multiple"] for r in rows) / n, "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None}

    # AUDITORÍA (08/09/2026): equivalente de stats_by_dimension (db.py) para
    # el backend de producción (Supabase) -- desglosa win rate/expectancy/
    # profit factor por symbol, setup_type o confidence (ver
    # analyze_crypto_setups.py). Mismo criterio de ganador por signo de
    # r_multiple que stats_summary() de acá arriba.
    def stats_by_dimension(self, field, since_ts=None):
        query = self.client.table("closed_trades").select(f"{field},outcome,r_multiple")
        if since_ts is not None:
            query = query.gte("ts_closed", since_ts)
        rows = [r for r in query.execute().data or [] if r.get("r_multiple") is not None]
        by_group = {}
        for r in rows:
            key = r.get(field) if r.get(field) is not None else "(sin dato)"
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

    def record_polymarket_signal(self, condition_id, question, direction, token_id, entry, target, stop, score=None, confidence=None):
        self.client.table("polymarket_signals").insert({"condition_id": condition_id, "question": question, "direction": direction, "token_id": token_id, "entry": entry, "target": target, "stop": stop, "score": score, "confidence": confidence, "ts_signaled": _now_iso()}).execute()

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

    def polymarket_recent_history(self, limit=20):
        """
        Semana 3.5: Historial detallado de señales de Polymarket con métricas
        de rendimiento real (ganancias/pérdidas, R-múltiple, tiempo de resolución).
        """
        rows = self.client.table("polymarket_signals").select(
            "condition_id,question,direction,entry,target,stop,outcome,exit_price,ts_signaled,ts_resolved"
        ).not_.is_("outcome", "null").order("ts_resolved", desc=True).limit(limit).execute().data or []
        
        history = []
        for r in rows:
            stop_distance = abs(r["entry"] - r["stop"])
            r_multiple = None
            profit_loss = None
            profit_pct = None
            
            # FIX (06/09/2026): sin el signo por direction -- entry/target/
            # stop ya están expresados en el precio de la punta elegida
            # (YES o NO), siempre subiendo si gana. Este era justo el bug
            # que ya se había señalado (sin corregir) en el comentario de
            # fetch_live_resolved() en polymarket_compare_backtest_live.py:
            # invertía el R-múltiplo de TODAS las señales NO (~mitad del
            # historial), mostrando wins reales como pérdidas y viceversa
            # en /api/polymarket_history.
            if stop_distance > 0 and r["exit_price"] is not None:
                r_multiple = (r["exit_price"] - r["entry"]) / stop_distance
                
                # Calcular ganancia/pérdida en USD (asumiendo $100 de riesgo base)
                base_risk = 100.0  # Puedes ajustar esto según tu tamaño de posición real
                profit_loss = r_multiple * base_risk
                profit_pct = r_multiple * 100
            
            # Calcular tiempo hasta resolución
            time_to_resolve = None
            if r["ts_signaled"] and r["ts_resolved"]:
                try:
                    from datetime import datetime
                    ts_sig = datetime.fromisoformat(r["ts_signaled"].replace('Z', '+00:00'))
                    ts_res = datetime.fromisoformat(r["ts_resolved"].replace('Z', '+00:00'))
                    time_to_resolve = (ts_res - ts_sig).total_seconds() / 3600  # horas
                except:
                    pass
            
            history.append({
                "question": r["question"][:80] if r["question"] else "Sin pregunta",
                "direction": r["direction"],
                "outcome": r["outcome"],
                "entry": r["entry"],
                "exit_price": r["exit_price"],
                "target": r["target"],
                "stop": r["stop"],
                "r_multiple": r_multiple,
                "profit_loss": profit_loss,
                "profit_pct": profit_pct,
                "time_to_resolve_hours": time_to_resolve,
                "ts_resolved": r["ts_resolved"],
            })
        
        return history


    def record_weather_signal(self, condition_id, question, event_title, station_icao, my_prob, market_price, ev, center_estimate_f, sigma, yes_token_id, stop=None):
        self.client.table("weather_signals").insert({"condition_id": condition_id, "question": question, "event_title": event_title, "station_icao": station_icao, "my_prob": my_prob, "market_price": market_price, "ev": ev, "center_estimate_f": center_estimate_f, "sigma": sigma, "yes_token_id": yes_token_id, "stop": stop, "ts_signaled": _now_iso()}).execute()

    def get_open_weather_signals(self):
        return self.client.table("weather_signals").select("*").is_("outcome", "null").execute().data or []

    def get_stopped_weather_condition_ids(self):
        """
        NUEVO (08/09/2026, evidencia real encontrada al retomar el audit de
        EV de clima): cada condition_id es un bucket puntual de un día
        específico -- una vez que se resolvió con "stop", ese día no va a
        volver a repetirse, así que no hace falta acotar por fecha. Se
        encontraron pares reales el 07/09 (misma pregunta, ej. "82-83°F
        NYC") comprados, parados, y comprados DE NUEVO más barato un par de
        horas después, parados otra vez -- el EV (my_prob/precio - 1) se
        infla solo cuando el precio de mercado cae más rápido que lo que
        el modelo actualiza my_prob entre ciclos, así que el bucket que
        acaba de derrumbarse se ve "más atractivo" en vez de correctamente
        menos probable. Un stop ya es la señal de que el mercado sabe algo
        que el modelo todavía no absorbió -- no se vuelve a entrar al
        mismo bucket ese día.
        """
        res = self.client.table("weather_signals").select("condition_id").eq("outcome", "stop").execute()
        return {r["condition_id"] for r in (res.data or [])}

    def count_weather_signals_for_event(self, station_icao, event_title):
        """
        NUEVO (07/09/2026, usuario reportó "compra varias veces al día
        para la misma ciudad a medida que sube la temperatura"): el guard
        de open_events en run_weather_cycle (app.py, 06/09/2026) evita
        tener DOS señales ABIERTAS a la vez para el mismo (estación,
        evento) -- pero no evita volver a abrir una señal nueva para el
        mismo (estación, evento) después de que la anterior ya se cerró
        (por el stop-loss agregado ayer, o por resolución completa). El
        precio de estos buckets se derrumba justo cuando el modelo
        "persigue" la temperatura real que sigue subiendo en la tarde
        (ver estimate_adjusted_high en weather_signal_engine.py) -- así
        que el patrón "abrir -> stop -> abrir el bucket de arriba -> stop
        -> abrir el de arriba..." puede repetirse varias veces el mismo
        día sin que el guard existente lo note, porque nunca hay dos
        señales abiertas AL MISMO TIEMPO.

        Esta cuenta es total (cualquier outcome, incluido abiertas) para
        poner un tope duro de intentos por (estación, evento) sin importar
        si siguen abiertas o ya se resolvieron.
        """
        res = (
            self.client.table("weather_signals")
            .select("id", count="exact")
            .eq("station_icao", station_icao)
            .eq("event_title", event_title)
            .execute()
        )
        return res.count or 0

    def resolve_weather_signal(self, signal_id, outcome, exit_price=None):
        # NUEVO (06/09/2026): exit_price opcional -- outcome="stop" lo pasa
        # (precio de salida anticipada, no siempre -100%), outcome="yes"/"no"
        # de una resolución completa normal no lo necesita (queda None).
        update = {"outcome": outcome, "ts_resolved": _now_iso()}
        if exit_price is not None:
            update["exit_price"] = exit_price
        res = self.client.table("weather_signals").update(update).eq("id", signal_id).is_("outcome", "null").execute()
        return bool(res.data)

    def record_mlb_signal(self, condition_id, game_pk, question, home_team, away_team, direction,
                           my_prob, market_price, ev, confidence, confidence_penalty, token_id, stop=None,
                           home_win_pct=None, away_win_pct=None, era_home=None, era_away=None,
                           pitcher_edge=None, home_field_edge=None):
        # AUDITORÍA (07/09/2026): se agregan los componentes de
        # estimate_win_probability() (home_win_pct/away_win_pct/era_home/
        # era_away/pitcher_edge/home_field_edge) -- antes solo se guardaba
        # my_prob final, así que al detectar mala calibración en 60-80% no
        # había forma de saber si el culpable era el ajuste de ERA, el de
        # localía, o ninguno de los dos (ruido de muestra chica). Todos
        # opcionales/None por defecto para no romper otros callers.
        self.client.table("mlb_signals").insert({"condition_id": condition_id, "game_pk": game_pk, "question": question, "home_team": home_team, "away_team": away_team, "direction": direction, "my_prob": my_prob, "market_price": market_price, "ev": ev, "confidence": confidence, "confidence_penalty": confidence_penalty, "token_id": token_id, "stop": stop, "home_win_pct": home_win_pct, "away_win_pct": away_win_pct, "era_home": era_home, "era_away": era_away, "pitcher_edge": pitcher_edge, "home_field_edge": home_field_edge, "ts_signaled": _now_iso()}).execute()

    def get_open_mlb_signals(self):
        return self.client.table("mlb_signals").select("*").is_("outcome", "null").execute().data or []

    def resolve_mlb_signal(self, signal_id, outcome, exit_price=None):
        # NUEVO (06/09/2026): mismo agregado que resolve_weather_signal --
        # outcome="stop" pasa exit_price, "win"/"loss" de resolución
        # completa normal no lo necesita.
        update = {"outcome": outcome, "ts_resolved": _now_iso()}
        if exit_price is not None:
            update["exit_price"] = exit_price
        res = self.client.table("mlb_signals").update(update).eq("id", signal_id).is_("outcome", "null").execute()
        return bool(res.data)
       
    def weather_calibration_summary(self, bucket_size=0.1):
        rows = self.client.table("weather_signals").select("my_prob,outcome").not_.is_("outcome", "null").execute().data or []
        n = len(rows)
        if n == 0: return {"n": 0, "brier_score": None, "buckets": []}
        buckets, brier_sum = {}, 0.0
        for r in rows:
            actual = 1.0 if r["outcome"] == "yes" else 0.0
            brier_sum += (r["my_prob"] - actual) ** 2
            # FIX (07/09/2026): +1e-9 antes del int() -- sin esto,
            # 0.30/0.1 da 2.9999999999999996 (error de punto flotante) y
            # una probabilidad de exactamente 30% caía en el bucket
            # "20-30%" en vez de "30-40%". Mismo fix aplicado en
            # mlb_calibration_summary() más abajo, que copia este patrón.
            key = min(int(r["my_prob"] / bucket_size + 1e-9), int(1 / bucket_size) - 1)
            b = buckets.setdefault(key, {"predicted": [], "actual": []})
            b["predicted"].append(r["my_prob"]); b["actual"].append(actual)
        bucket_rows = [{"range": f"{k*bucket_size*100:.0f}-{(k+1)*bucket_size*100:.0f}%", "n": len(b["predicted"]), "avg_predicted": sum(b["predicted"])/len(b["predicted"]), "actual_freq": sum(b["actual"])/len(b["actual"])} for k, b in sorted(buckets.items())]
        return {"n": n, "brier_score": brier_sum / n, "buckets": bucket_rows}

    def mlb_calibration_summary(self, bucket_size=0.1):
        """
        NUEVO (07/09/2026, a pedido del usuario): equivalente de
        weather_calibration_summary() para MLB -- responde "cuando el
        modelo dice que el lado elegido tiene X% de ganar, ¿de verdad gana
        cerca de X% de las veces?". Se agrupan las señales resueltas por
        rango de `my_prob` (la probabilidad del LADO QUE SE COMPRÓ, ya
        armada así en mlb_signal_engine.py -- no la del "home team" a
        secas) y se compara contra la frecuencia real de victorias en cada
        rango.

        Solo cuenta outcome "win"/"loss": un "stop" corta la posición
        antes de que el partido termine, así que no sabemos si el lado
        elegido hubiera ganado o perdido en realidad; un "void" es un
        partido que no se llegó a jugar. Ninguno de los dos dice nada
        sobre si la probabilidad estaba bien calculada.
        """
        rows = (
            self.client.table("mlb_signals")
            .select("my_prob,outcome")
            .in_("outcome", ["win", "loss"])
            .execute()
            .data or []
        )
        n = len(rows)
        if n == 0:
            return {"n": 0, "brier_score": None, "buckets": []}
        buckets, brier_sum = {}, 0.0
        for r in rows:
            if r["my_prob"] is None:
                continue
            actual = 1.0 if r["outcome"] == "win" else 0.0
            brier_sum += (r["my_prob"] - actual) ** 2
            key = min(int(r["my_prob"] / bucket_size + 1e-9), int(1 / bucket_size) - 1)
            b = buckets.setdefault(key, {"predicted": [], "actual": []})
            b["predicted"].append(r["my_prob"])
            b["actual"].append(actual)
        bucket_rows = [
            {
                "range": f"{k*bucket_size*100:.0f}-{(k+1)*bucket_size*100:.0f}%",
                "n": len(b["predicted"]),
                "avg_predicted": sum(b["predicted"]) / len(b["predicted"]),
                "actual_freq": sum(b["actual"]) / len(b["actual"]),
            }
            for k, b in sorted(buckets.items())
        ]
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
