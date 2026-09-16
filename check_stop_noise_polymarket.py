"""
check_stop_noise_polymarket.py — ¿Los stops del módulo Polymarket genérico
están cortando señales que igual hubieran resuelto a favor?

Mismo criterio que ya usa el propio bot para el fallback de "cierre sin
detección a tiempo" (ver check_open_signals en polymarket_track_results.py):
una vez que el mercado cierra de verdad, el yes_price final (ajustado por
dirección) dice si el resultado real terminó del lado de la entrada o no.
Acá se aplica ese mismo chequeo, pero a señales que SÍ se cerraron por
stop, para ver si el mercado, después de tocar el stop, terminó resolviendo
igual a favor.

CÓMO CORRERLO
-------------
Mismo esquema que los otros dos scripts de esta auditoría -- agregar un paso
más al workflow check-stop-noise.yml, o correr suelto:

    python check_stop_noise_polymarket.py [--output resultados.csv]

Requiere SUPABASE_URL / SUPABASE_KEY. Pega a la CLOB API de Polymarket
(pública, sin auth) vía PolymarketClient -- no toca cripto ni el exchange.

LIMITACIÓN CONOCIDA (ver auditoría de MLB/clima, mismo hallazgo): si el
mercado ya cerró hace mucho y Polymarket podó su historial fino,
fetch_clob_market() igual sirve acá porque solo pide el ESTADO FINAL
(closed + yes_price), no la serie de precios -- eso es justo lo que
funcionaba bien en el fallback de "cierre tardío" del propio bot, a
diferencia de fetch_price_history() que sí se rompe para mercados viejos.
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
from polymarket_client import PolymarketClient  # noqa: E402


def fetch_stopped(supabase):
    resp = (
        supabase.table("polymarket_signals")
        .select("id,condition_id,question,direction,entry,target,stop,exit_price,ts_signaled")
        .eq("outcome", "stop")
        .order("ts_signaled", desc=False)
        .execute()
    )
    return resp.data or []


def check(supabase, client):
    rows = fetch_stopped(supabase)
    print(f"=== POLYMARKET: {len(rows)} señales cerradas por stop ===\n")
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

        final_yes = market.get("yes_price")
        if final_yes is None:
            print(f"  [SKIP] {r['question'][:50]}: sin yes_price final")
            continue

        # Mismo ajuste por dirección que usa el propio bot en
        # check_open_signals (rama de cierre tardío).
        final_price = final_yes if r["direction"] == "YES" else (1.0 - final_yes)
        would_have_won = final_price >= r["entry"]

        results.append({
            "modulo": "polymarket", "pregunta": r["question"][:70],
            "direccion": r["direction"], "ts_signaled": r["ts_signaled"],
            "entry": r["entry"], "stop": r["stop"], "final_price": round(final_price, 3),
            "hubiera_ganado": would_have_won,
        })
        flag = "⚠️ HUBIERA GANADO" if would_have_won else "ok (perdía igual)"
        print(f"  [{r['ts_signaled']}] {r['question'][:60]} ({r['direction']}) "
              f"entry={r['entry']:.3f} final={final_price:.3f} → {flag}")
        time.sleep(0.3)
    return results


def summarize(results):
    if not results:
        print("\nSin datos suficientes para concluir (ningún mercado cerrado todavía, o todos fallaron).")
        return
    n_recovered = sum(1 for r in results if r["hubiera_ganado"])
    pct = 100 * n_recovered / len(results)
    print(f"\n=== RESUMEN ===")
    print(f"Analizados: {len(results)}")
    print(f"Hubieran resuelto a favor igual (posible corte de más): {n_recovered} ({pct:.1f}%)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", type=str, default=None, help="CSV opcional con el detalle")
    args = ap.parse_args()

    supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    client = PolymarketClient(Config)

    results = check(supabase, client)
    summarize(results)

    if args.output and results:
        with open(args.output, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(results[0].keys()))
            writer.writeheader()
            writer.writerows(results)
        print(f"\nDetalle guardado en {args.output}")


if __name__ == "__main__":
    sys.exit(main())
