"""
Cruza el CSV que produce polymarket_backtest.py (--output) contra las
señales de polymarket_signals YA resueltas en vivo (paper trading real, no
simulación) -- para responder "¿el edge que mide el backtest histórico se
parece al que el bot está teniendo de verdad?".

Son dos mediciones distintas y ninguna reemplaza a la otra:
- Backtest: generate_polymarket_signal() caminado sobre TODO el historial
  de mercados cerrados encontrados, cerrando cada trade en el primer cruce
  de target/stop dentro de esa simulación.
- Live: las señales que el bot realmente mandó y que ya se resolvieron --
  algunas por cruce de precio (igual que el backtest) pero otras por el
  fallback de "mercado cerrado sin stop detectado a tiempo" (ver hallazgo
  03/09/2026 en polymarket_track_results.py), que no tiene equivalente en
  el backtest y puede in flar o desinflar el R real de esos casos.

Compara por:
- confidence (1-5, mismo campo en las dos fuentes)
- category (el backtest ya la trae calculada; a las señales live se les
  aplica categorize() acá mismo, la MISMA función que usa producción, para
  que no se comparen categorizaciones distintas)

Uso:
    python polymarket_compare_backtest_live.py --backtest-csv resultados_pm.csv
    python polymarket_compare_backtest_live.py --backtest-csv resultados_pm.csv --output comparacion.csv
"""
import argparse
import csv
import logging
import os

from supabase import create_client

from polymarket_categories import categorize

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("polymarket_compare_backtest_live")


def load_backtest_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["r_multiple"] = float(r["r_multiple"])
        r["confidence"] = int(r["confidence"])
    return rows


def fetch_live_resolved(client):
    """
    Mismo criterio de r_multiple que polymarket_stats_summary() en
    supabase_db.py (sign=+1 siempre) -- NO el de polymarket_recent_history()
    (que multiplica por -1 si direction=="NO"). entry/target/stop ya están
    expresados en el precio de LA PUNTA elegida (YES o NO), igual que en
    polymarket_backtest.py ("el precio de la punta elegida siempre sube si
    gana, baja si pierde") -- aplicar un signo extra por dirección ahí
    duplicaría el ajuste y invertiría el R de las señales NO. Se usa la
    convención del backtest para que la comparación sea manzanas-con-manzanas;
    si algún reporte de producción difiere de este número, es la otra
    función (polymarket_recent_history) la que hay que revisar, no esta.
    """
    rows = (
        client.table("polymarket_signals")
        .select("question,direction,entry,target,stop,outcome,exit_price,confidence")
        .not_.is_("outcome", "null")
        .execute()
        .data
        or []
    )
    out = []
    skipped = 0
    for r in rows:
        stop_distance = abs(r["entry"] - r["stop"])
        if stop_distance <= 0 or r["exit_price"] is None or r["confidence"] is None:
            skipped += 1
            continue
        r_multiple = (r["exit_price"] - r["entry"]) / stop_distance
        out.append({
            "question": r["question"] or "",
            "category": categorize(r["question"] or ""),
            "confidence": int(r["confidence"]),
            "outcome": r["outcome"],
            "r_multiple": r_multiple,
        })
    if skipped:
        log.info(f"{skipped} señal(es) live resuelta(s) sin stop_distance/exit_price/confidence válidos -- excluidas.")
    return out


def summarize(rows):
    if not rows:
        return {"n": 0, "win_rate": None, "expectancy_r": None, "profit_factor": None, "total_r": 0.0}
    wins = [r for r in rows if r["outcome"] == "target"]
    win_rate = len(wins) / len(rows) * 100
    total_r = sum(r["r_multiple"] for r in rows)
    expectancy = total_r / len(rows)
    gross_win = sum(r["r_multiple"] for r in rows if r["r_multiple"] > 0)
    gross_loss = abs(sum(r["r_multiple"] for r in rows if r["r_multiple"] < 0))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
    return {"n": len(rows), "win_rate": win_rate, "expectancy_r": expectancy,
            "profit_factor": profit_factor, "total_r": total_r}


def group_by(rows, key):
    groups = {}
    for r in rows:
        groups.setdefault(r[key], []).append(r)
    return groups


def fmt(s):
    if s["n"] == 0:
        return "sin datos"
    pf = f"{s['profit_factor']:.2f}" if s["profit_factor"] != float("inf") else "inf"
    return f"n={s['n']:>3} | win {s['win_rate']:5.1f}% | expectancy {s['expectancy_r']:+.2f}R | PF {pf} | total {s['total_r']:+.1f}R"


def compare_dimension(label, backtest_rows, live_rows, key, flag_threshold_pp=15.0):
    bt_groups = group_by(backtest_rows, key)
    live_groups = group_by(live_rows, key)
    all_keys = sorted(set(bt_groups) | set(live_groups), key=lambda k: str(k))

    log.info(f"=== Por {label} ===")
    flags = []
    for k in all_keys:
        bt = summarize(bt_groups.get(k, []))
        live = summarize(live_groups.get(k, []))
        log.info(f"  {label}={k}")
        log.info(f"    backtest: {fmt(bt)}")
        log.info(f"    live:     {fmt(live)}")
        if bt["n"] >= 5 and live["n"] >= 5:
            diff = abs(bt["win_rate"] - live["win_rate"])
            if diff >= flag_threshold_pp:
                flags.append((k, diff, bt["win_rate"], live["win_rate"]))
    return flags


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backtest-csv", required=True, help="CSV generado por polymarket_backtest.py --output")
    parser.add_argument("--flag-threshold-pp", type=float, default=15.0,
                         help="diferencia de win rate (puntos porcentuales) para marcar un grupo como divergente")
    parser.add_argument("--output", help="CSV opcional con el resumen por categoría/confianza")
    args = parser.parse_args()

    backtest_rows = load_backtest_csv(args.backtest_csv)
    log.info(f"{len(backtest_rows)} trade(s) simulado(s) cargados de {args.backtest_csv}")

    client = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    live_rows = fetch_live_resolved(client)
    log.info(f"{len(live_rows)} señal(es) live resuelta(s) traídas de Supabase")

    log.info("=== Total general ===")
    bt_total = summarize(backtest_rows)
    live_total = summarize(live_rows)
    log.info(f"  backtest: {fmt(bt_total)}")
    log.info(f"  live:     {fmt(live_total)}")

    flags = []
    flags += compare_dimension("confidence", backtest_rows, live_rows, "confidence", args.flag_threshold_pp)
    flags += compare_dimension("category", backtest_rows, live_rows, "category", args.flag_threshold_pp)

    if flags:
        log.warning(f"=== Grupos con diferencia de win rate >= {args.flag_threshold_pp:.0f}pp entre backtest y live ===")
        for k, diff, bt_wr, live_wr in flags:
            log.warning(f"  {k}: backtest {bt_wr:.1f}% vs live {live_wr:.1f}% (diferencia {diff:.1f}pp)")
        log.warning(
            "Una diferencia grande no confirma un bug por sí sola -- puede ser tamaño de muestra chico, "
            "el fallback de 'cerrado sin stop detectado' (solo existe en live, no en el backtest), o que "
            "el book real (verify_entry_against_book) filtró en vivo señales que el backtest sí cuenta. "
            "Vale la pena mirar el detalle de esos grupos antes de sacar conclusiones."
        )
    else:
        log.info("Sin grupos con diferencia de win rate por encima del umbral -- backtest y live cuentan una historia parecida.")

    if args.output:
        with open(args.output, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["dimension", "key", "source", "n", "win_rate", "expectancy_r", "profit_factor", "total_r"])
            for label, key in (("confidence", "confidence"), ("category", "category")):
                bt_groups = group_by(backtest_rows, key)
                live_groups = group_by(live_rows, key)
                for k in sorted(set(bt_groups) | set(live_groups), key=lambda x: str(x)):
                    for source, groups in (("backtest", bt_groups), ("live", live_groups)):
                        s = summarize(groups.get(k, []))
                        pf = s["profit_factor"] if s["n"] else None
                        writer.writerow([label, k, source, s["n"], s["win_rate"], s["expectancy_r"], pf, s["total_r"]])
        log.info(f"Resumen guardado en {args.output}")


if __name__ == "__main__":
    main()
