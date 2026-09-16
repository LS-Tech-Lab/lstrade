"""
check_stop_noise_weather_mlb.py — ¿Los stops de clima y MLB están cortando
señales que igual hubieran resuelto a favor?

A diferencia de cripto (donde hay que reconstruir si el precio habría
tocado el target), acá es más directo: el evento real (clima del día,
resultado del partido) ya pasó y es un hecho consultable, sin importar
cuándo el bot cerró la posición por stop. Reusa las mismas funciones que
ya usa el repo para resolver señales de verdad:

  - Clima: PolymarketClient.fetch_clob_market(condition_id) → yes_price
    final del mercado ya cerrado (mismo criterio que run_weather_track_results
    en app.py: >=0.98 → YES, <=0.02 → NO). Las señales de clima siempre
    compran YES (barato/infravalorado), así que "hubiera ganado" = el
    mercado terminó resolviendo YES.
  - MLB: mlb_signal_engine.fetch_game_result(game_pk) → resultado real del
    partido (ya jugado). Se compara contra sig["direction"] (YES=local,
    NO=visitante) para ver si el lado comprado hubiera ganado.

CÓMO CORRERLO
-------------
Mismo esquema que check_stop_noise.py: copiar a la raíz del repo y correr
con el mismo .env / mismo workflow de GitHub Actions (ver
check-stop-noise.yml — se puede agregar un job más o correr este script
como paso adicional).

    python check_stop_noise_weather_mlb.py [--output resultados.csv]

Requiere SUPABASE_URL / SUPABASE_KEY. No necesita API_KEY/API_SECRET del
exchange (no toca cripto). Sí pega a la CLOB API de Polymarket y a la MLB
Stats API — ambas públicas, sin auth.
"""
import argparse
import csv
import os
import sys
import time

from dotenv import load_dotenv

load_dotenv()

from config import Config              # noqa: E402
from supabase import create_client     # noqa: E402
from polymarket_client import PolymarketClient           # noqa: E402
from mlb_signal_engine import fetch_game_result           # noqa: E402

# Mismos umbrales que usa run_weather_track_results() en app.py para
# decidir si un mercado ya cerrado resolvió YES o NO.
WEATHER_RESOLVED_YES_THRESHOLD = 0.98
WEATHER_RESOLVED_NO_THRESHOLD = 0.02


def fetch_stopped(supabase, table, columns):
    resp = (
        supabase.table(table)
        .select(columns)
        .eq("outcome", "stop")
        .order("ts_signaled", desc=False)
        .execute()
    )
    return resp.data or []


def check_weather(supabase, client):
    rows = fetch_stopped(
        supabase, "weather_signals",
        "id,condition_id,question,station_icao,target_date,exit_price,ts_signaled"
    )
    print(f"\n=== CLIMA: {len(rows)} señales cerradas por stop ===")
    results = []
    for r in rows:
        try:
            market = client.fetch_clob_market(r["condition_id"])
        except Exception as e:
            print(f"  [WARN] {r['question'][:50]}: no se pudo consultar el mercado ({e})")
            continue
        if not market or not market.get("closed"):
            print(f"  [SKIP] {r['question'][:50]}: el mercado todavía no cerró de verdad")
            continue
        yes_price = market.get("yes_price", 0.0)
        if yes_price >= WEATHER_RESOLVED_YES_THRESHOLD:
            real_outcome = "yes"
        elif yes_price <= WEATHER_RESOLVED_NO_THRESHOLD:
            real_outcome = "no"
        else:
            real_outcome = f"ambiguo ({yes_price:.3f})"
        would_have_won = real_outcome == "yes"  # clima siempre compra YES
        results.append({
            "modulo": "weather", "pregunta": r["question"][:70],
            "ts_signaled": r["ts_signaled"], "resultado_real": real_outcome,
            "hubiera_ganado": would_have_won,
        })
        flag = "⚠️ HUBIERA GANADO" if would_have_won else "ok (perdía igual)"
        print(f"  [{r['ts_signaled']}] {r['question'][:60]} → {flag} (resuelto: {real_outcome})")
        time.sleep(0.3)
    return results


def check_mlb(supabase):
    rows = fetch_stopped(
        supabase, "mlb_signals",
        "id,condition_id,question,game_pk,direction,home_team,away_team,exit_price,ts_signaled"
    )
    print(f"\n=== MLB: {len(rows)} señales cerradas por stop ===")
    results = []
    for r in rows:
        game_pk = r.get("game_pk")
        if not game_pk:
            continue
        result = fetch_game_result(game_pk)
        if not result:
            print(f"  [SKIP] {r['question'][:50]}: partido todavía no figura como terminado")
            continue
        if result.get("voided"):
            print(f"  [SKIP] {r['question'][:50]}: partido cancelado/sin resultado jugado")
            continue
        bought_home = r.get("direction") == "YES"
        won = result["home_won"] if bought_home else result["away_won"]
        results.append({
            "modulo": "mlb", "pregunta": r["question"][:70],
            "ts_signaled": r["ts_signaled"], "resultado_real": "win" if won else "loss",
            "hubiera_ganado": won,
        })
        flag = "⚠️ HUBIERA GANADO" if won else "ok (perdía igual)"
        print(f"  [{r['ts_signaled']}] {r['home_team']} vs {r['away_team']} ({r['direction']}) → {flag}")
        time.sleep(0.3)
    return results


def summarize(label, results):
    if not results:
        print(f"\n{label}: sin datos suficientes para concluir.")
        return
    n_recovered = sum(1 for r in results if r["hubiera_ganado"])
    pct = 100 * n_recovered / len(results)
    print(f"\n{label}: {n_recovered}/{len(results)} stops ({pct:.1f}%) hubieran resuelto a favor "
          f"si no se hubieran cortado por stop.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", type=str, default=None, help="CSV opcional con el detalle combinado")
    args = ap.parse_args()

    supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    client = PolymarketClient(Config)

    weather_results = check_weather(supabase, client)
    mlb_results = check_mlb(supabase)

    summarize("CLIMA", weather_results)
    summarize("MLB", mlb_results)

    all_results = weather_results + mlb_results
    if args.output and all_results:
        with open(args.output, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(all_results[0].keys()))
            writer.writeheader()
            writer.writerows(all_results)
        print(f"\nDetalle guardado en {args.output}")


if __name__ == "__main__":
    sys.exit(main())
