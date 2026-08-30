"""
Versión serverless de polymarket_main.py. Recortada a propósito para caber
en el timeout de 10s del plan Hobby de Vercel:
  - Escanea muchos menos mercados (POLYMARKET_TOP_N, default 5 acá vs. 20
    por default en el modo local).
  - Manda como máximo 1 señal por invocación, no 3.
  - No genera ni envía el gráfico (matplotlib + la llamada a Telegram con
    la imagen agregan latencia que no vale la pena arriesgar acá) — el
    memo en texto sigue teniendo todos los datos. El modo local sigue
    mandando el gráfico sin problema porque no tiene ese límite de tiempo.

Antes este módulo no existía en el modo serverless: Polymarket solo corría
si tenías polymarket_main.py prendido en una terminal/VPS aparte. Esto lo
integra al mismo esquema de GitHub Actions + Vercel que ya usa el ciclo de
cripto (ver .github/workflows/trigger-polymarket-cycle.yml).
"""
from http.server import BaseHTTPRequestHandler
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Config
from supabase_db import SupabaseDatabase
from polymarket_client import PolymarketClient
from polymarket_signal_engine import generate_polymarket_signal
from telegram_notifier import TelegramNotifier


def build_memo_markdown(signal):
    m = signal["market"]
    lines = [f"🎯 *SEÑAL POLYMARKET* — {m['question'][:70]}"]
    lines.append(f"📊 Dirección: {signal['direction']} (confianza {signal['confidence']}/5)")
    lines.append(f"💰 Precio YES: ${m['yes_price']:.3f} | NO: ${m['no_price']:.3f}")
    lines.append(f"💧 Liquidez: ${m['liquidity']:,.2f}")
    tp = signal.get("trade_plan")
    if tp:
        lines.append(f"🎯 Entrada: ${tp['entry']:.3f} | Target: ${tp['target']:.3f} | Stop: ${tp['stop']:.3f}")
    lines.append("")
    lines.append("_⚠️ MODO LECTURA — No se ejecutó ninguna operación real._")
    return "\n".join(lines)


def run_polymarket_cycle():
    config = Config
    db = SupabaseDatabase(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    client = PolymarketClient(config)
    notifier = TelegramNotifier(config)

    top_n = int(os.environ.get("POLYMARKET_TOP_N", "5"))
    markets_raw = client.fetch_active_markets(limit=top_n)
    if not markets_raw:
        return {"status": "no_markets"}

    market_by_condition_id = {}
    signals = []
    for market_raw in markets_raw:
        market = client.parse_market_for_analysis(market_raw)
        if not market or market["liquidity"] < 1000 or not market.get("yes_token_id"):
            continue
        market_by_condition_id[market["condition_id"]] = market

        price_history = client.fetch_price_history(market["yes_token_id"], interval="1h", fidelity=60)
        signal = generate_polymarket_signal(
            market, price_history,
            stop_vol_mult=config.POLYMARKET_STOP_VOL_MULT,
            target_rr=config.POLYMARKET_TARGET_RR,
        )
        if signal:
            signals.append(signal)

    if not signals:
        return {"status": "no_signal", "markets_scanned": len(markets_raw)}

    signals.sort(key=lambda s: s["score"], reverse=True)
    best = signals[0]

    notifier.send_message(build_memo_markdown(best))

    if best.get("trade_plan"):
        condition_id = best["market"]["condition_id"]
        original_market = market_by_condition_id.get(condition_id, {})
        token_id = original_market.get("yes_token_id") if best["direction"] == "YES" \
            else original_market.get("no_token_id")
        if token_id:
            tp = best["trade_plan"]
            db.record_polymarket_signal(
                condition_id, best["market"]["question"], best["direction"], token_id,
                tp["entry"], tp["target"], tp["stop"],
            )

    return {"status": "signal_sent", "condition_id": best["market"]["condition_id"], "direction": best["direction"]}


class handler(BaseHTTPRequestHandler):
    def _respond(self, code, payload):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def do_POST(self):
        self.do_GET()

    def do_GET(self):
        expected = os.environ.get("CRON_SECRET")
        auth = self.headers.get("Authorization", "")
        if expected and auth != f"Bearer {expected}":
            self._respond(401, {"error": "unauthorized"})
            return
        try:
            result = run_polymarket_cycle()
            self._respond(200, result)
        except Exception as e:
            self._respond(500, {"status": "error", "detail": str(e)})
