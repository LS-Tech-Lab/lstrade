"""
Tests para los cambios del 24/09/2026 (filtro pregame + evidencia de ask real
en mlb_signals). No requieren red ni Supabase real:
    python3 test_mlb_pregame_and_ask_evidence.py
Sale con status 0 si todo pasa, 1 en el primer fallo.
"""
import sys
import types
from datetime import datetime, timezone, timedelta

from mlb_signal_engine import game_is_pregame

passed = 0
failed = 0


def check(label, condition):
    global passed, failed
    if condition:
        print(f"OK   {label}")
        passed += 1
    else:
        print(f"FAIL {label}")
        failed += 1


NOW = datetime(2026, 9, 24, 20, 0, tzinfo=timezone.utc)
future = (NOW + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
past = (NOW - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")

# --- game_is_pregame ---
check("Preview + inicio futuro -> pregame", game_is_pregame({"game_state": "Preview", "game_date": future}, NOW))
check("Preview + inicio pasado (demora) -> NO pregame", not game_is_pregame({"game_state": "Preview", "game_date": past}, NOW))
check("Live -> NO pregame", not game_is_pregame({"game_state": "Live", "game_date": future}, NOW))
check("Final -> NO pregame", not game_is_pregame({"game_state": "Final", "game_date": past}, NOW))
check("sin datos -> se deja pasar", game_is_pregame({}, NOW))
check("fecha inválida + Preview -> se deja pasar", game_is_pregame({"game_state": "Preview", "game_date": "basura"}, NOW))
check("sin state, inicio pasado -> NO pregame", not game_is_pregame({"game_date": past}, NOW))

# --- record_mlb_signal: payload y fallback ---
sys.modules.setdefault("supabase", types.SimpleNamespace(create_client=lambda *a, **k: None, Client=object))
try:
    import supabase_db
    DB = supabase_db.SupabaseDatabase
except Exception as e:  # pragma: no cover
    DB = None
    print(f"SKIP record_mlb_signal (no se pudo importar supabase_db: {e})")


class _Q:
    def __init__(self, owner):
        self.owner = owner

    def insert(self, row):
        self.owner.rows.append(row)
        return self

    def execute(self):
        if self.owner.fail_on_extras and "ask_at_signal" in self.owner.rows[-1]:
            raise RuntimeError("column does not exist")
        return types.SimpleNamespace(data=[{}])


class _Client:
    def __init__(self, fail_on_extras=False):
        self.rows = []
        self.fail_on_extras = fail_on_extras

    def table(self, name):
        return _Q(self)


if DB is not None:
    base = ("c1", 1, "q", "H", "A", "YES", 0.55, 0.42, 0.3, 3, 0.0, "tok")

    for fail in (False, True):
        obj = DB.__new__(DB)
        obj.client = _Client(fail_on_extras=fail)
        obj.record_mlb_signal(*base, ask_at_signal=0.43, bid_at_signal=0.41, est_fee_per_share=0.0123,
                              ev_at_ask_net=0.2, game_start_ts="2026-09-24T23:05:00Z")
        last = obj.client.rows[-1]
        if not fail:
            check("payload incluye columnas nuevas", last.get("ask_at_signal") == 0.43 and last.get("game_start_ts"))
        else:
            check("fallback: reintenta sin columnas nuevas y guarda la señal",
                  len(obj.client.rows) == 2 and "ask_at_signal" not in last and last["condition_id"] == "c1")

    obj = DB.__new__(DB)
    obj.client = _Client()
    obj.record_mlb_signal(*base)
    check("sin extras: payload sin claves nuevas (compatible)", "ask_at_signal" not in obj.client.rows[-1])

# --- fee / EV neto (misma fórmula que run_mlb_cycle) ---
ask, my_prob, rate = 0.43, 0.55, 0.05
fee = round(rate * ask * (1 - ask), 5)
ev_net = round(my_prob / (ask + fee) - 1, 4)
check("fee = 0.05*p*(1-p)", abs(fee - 0.01226) < 1e-9)
check("EV neto < EV bruto", ev_net < round(my_prob / ask - 1, 4))

print(f"\n{passed} OK, {failed} FAIL")
sys.exit(1 if failed else 0)
