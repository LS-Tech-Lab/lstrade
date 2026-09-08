"""
Desglosa el performance real de cripto (closed_trades en Supabase) por
setup_type, confidence y símbolo -- el mismo ejercicio que ya se hizo por
categoría en Polymarket (analyze_polymarket_categories.py) y por componente
de probabilidad en MLB (columnas home_win_pct/era_home/... en mlb_signals).

Antes de este script, el dashboard solo mostraba un número agregado (34.4%
win rate / 1.64 profit factor con el criterio viejo, 51.7%/1.93 con el
criterio corregido de r_multiple -- ver fix en dashboard/app/api/data/route.js
del 08/09/2026) sin forma de saber si esos números escondían un tipo de
setup con edge real mezclado con otro que lo arrastra para abajo.

Requiere que add_open_trade() ya venga guardando setup_type/confidence/score
(propagado desde best_signal en app.py/main.py, cambio del 08/09/2026) --
los trades cerrados ANTES de ese cambio salen agrupados bajo "(sin dato)".

Uso:
    python analyze_crypto_setups.py                  # todo el historial
    python analyze_crypto_setups.py --days 14         # últimos 14 días
    python analyze_crypto_setups.py --min-n 5         # umbral de muestra mínima
"""
import argparse
import os
import time

from supabase_db import SupabaseDatabase

DIMENSIONS = [
    ("setup_type", "SETUP TYPE"),
    ("confidence", "CONFIDENCE"),
    ("symbol", "SÍMBOLO"),
]


def format_stats(key, s, min_n):
    flag = "" if s["n"] >= min_n else "  ⚠ muestra insuficiente"
    pf = f"{s['profit_factor']:.2f}" if s["profit_factor"] is not None else "—"
    return (
        f"  {str(key):<24} n={s['n']:<4} win={s['win_rate']:5.1f}%  "
        f"expectancy={s['expectancy_r']:+.2f}R  total={s['total_r']:+.2f}R  PF={pf}{flag}"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=None,
                         help="ventana en días hacia atrás (default: todo el historial)")
    parser.add_argument("--min-n", type=int, default=5,
                         help="grupos con menos trades que esto se muestran pero marcados como muestra insuficiente")
    args = parser.parse_args()

    db = SupabaseDatabase(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

    # NOTA: stats_by_dimension()/stats_summary() de supabase_db.py filtran
    # por ts_closed >= since_ts, que ahí espera un timestamp en formato ISO
    # (ver _now_iso() en el propio módulo), no epoch -- a diferencia de
    # db.py (SQLite), que sí usa epoch. Se arma acá con time.strftime en UTC.
    since_ts = None
    if args.days is not None:
        since_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - args.days * 86400))

    overall = db.stats_summary(since_ts=since_ts)
    if overall["n"] == 0:
        print("Sin trades cerrados en este período.")
        return

    window_label = f"últimos {args.days} días" if args.days else "todo el historial"
    print(f"\n=== CRIPTO — {window_label} ===")
    print(f"Total: n={overall['n']}  win={overall['win_rate']:.1f}%  "
          f"expectancy={overall['expectancy_r']:+.2f}R  "
          f"PF={overall['profit_factor']:.2f}" if overall["profit_factor"] is not None
          else f"Total: n={overall['n']}  win={overall['win_rate']:.1f}%  expectancy={overall['expectancy_r']:+.2f}R  PF=—")

    for field, label in DIMENSIONS:
        by_group = db.stats_by_dimension(field, since_ts=since_ts)
        if not by_group:
            continue
        print(f"\n--- Por {label} ---")
        # Ordenado por total_r ascendente: lo que más está arrastrando el
        # promedio para abajo queda arriba, a la vista.
        for key, s in sorted(by_group.items(), key=lambda kv: kv[1]["total_r"]):
            print(format_stats(key, s, args.min_n))

    print(
        "\nGrupos marcados con ⚠ tienen muestra insuficiente (< "
        f"{args.min_n} trades) -- dirección orientativa, no concluyente todavía.\n"
        "Un grupo con n suficiente y total_r consistentemente negativo es "
        "candidato a excluirse (mismo criterio que POLYMARKET_EXCLUDED_CATEGORIES "
        "en config.py) o a subirle el confidence mínimo requerido en signal_engine.py.\n"
    )


if __name__ == "__main__":
    main()
