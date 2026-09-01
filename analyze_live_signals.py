"""
Desglosa el desempeño real (no backtest) de polymarket_signals en Supabase,
por categoría — mismo criterio que analyze_polymarket_categories.py, pero
leyendo directo de la tabla en vez de un CSV de polymarket_backtest.py.

Usa esto para decidir qué agregar/sacar de
config.POLYMARKET_EXCLUDED_CATEGORIES con datos reales de producción, no
solo del backtest offline.

r_multiple no está guardado en polymarket_signals, así que se recalcula acá
con la misma fórmula que polymarket_backtest.py:
    r = (exit_price - entry) / abs(entry - stop)

Uso:
    export SUPABASE_URL=...
    export SUPABASE_KEY=...
    python analyze_live_signals.py
    python analyze_live_signals.py --min-n 10   # umbral de "muestra chica"
"""
import argparse
import os

from supabase import create_client

from polymarket_categories import categorize


def fetch_resolved_signals(client):
    res = (
        client.table("polymarket_signals")
        .select("question,entry,target,stop,outcome,exit_price")
        .not_.is_("outcome", "null")
        .execute()
    )
    return res.data or []


def summarize(rows):
    n = len(rows)
    if n == 0:
        return {"n": 0}
    r_multiples = [r["r_multiple"] for r in rows]
    wins = [r for r in rows if r["outcome"] == "target"]
    gross_win = sum(rm for rm in r_multiples if rm > 0)
    gross_loss = abs(sum(rm for rm in r_multiples if rm < 0))
    return {
        "n": n,
        "win_rate": len(wins) / n * 100,
        "expectancy_r": sum(r_multiples) / n,
        "total_r": sum(r_multiples),
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else float("inf"),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-n", type=int, default=5,
                         help="categorías con menos señales que esto se muestran pero marcadas como muestra insuficiente")
    args = parser.parse_args()

    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_KEY"]
    client = create_client(url, key)

    rows = fetch_resolved_signals(client)
    if not rows:
        print("Sin señales resueltas todavía en polymarket_signals.")
        return

    for r in rows:
        stop_distance = abs(r["entry"] - r["stop"])
        r["r_multiple"] = (r["exit_price"] - r["entry"]) / stop_distance if stop_distance > 0 else 0.0
        r["category"] = categorize(r["question"])

    by_category = {}
    for r in rows:
        by_category.setdefault(r["category"], []).append(r)

    overall = summarize(rows)
    print(f"\n=== TOTAL: {overall['n']} señales resueltas | win rate {overall['win_rate']:.1f}% | "
          f"expectancy {overall['expectancy_r']:+.2f}R | profit factor {overall['profit_factor']:.2f} | "
          f"total {overall['total_r']:+.2f}R ===\n")

    ranked = sorted(by_category.items(), key=lambda kv: summarize(kv[1])["total_r"], reverse=True)
    print(f"{'Categoría':32s} {'n':>5s} {'Win%':>7s} {'Expect.R':>10s} {'PF':>6s} {'Total R':>9s}")
    print("-" * 75)
    for cat, cat_rows in ranked:
        s = summarize(cat_rows)
        flag = " ⚠️ muestra chica" if s["n"] < args.min_n else ""
        pf = f"{s['profit_factor']:.2f}" if s["profit_factor"] != float("inf") else "inf"
        print(f"{cat:32s} {s['n']:>5d} {s['win_rate']:>6.1f}% {s['expectancy_r']:>+9.2f}R {pf:>6s} {s['total_r']:>+8.2f}R{flag}")

    print(f"\nconfig.POLYMARKET_EXCLUDED_CATEGORIES actual: agregá acá las categorías con\n"
          f"total R negativo y muestra suficiente (n >= {args.min_n}). Con menos que eso, el\n"
          f"resultado puede ser ruido — no lo trates como evidencia todavía.")


if __name__ == "__main__":
    main()
