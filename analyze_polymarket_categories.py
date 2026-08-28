"""
Desglosa resultados_pm.csv (el output de polymarket_backtest.py) por
categoría de mercado — la API de Gamma no expone una categoría estructurada
confiable en lo que ya trae parse_market_for_analysis, así que se infiere
por keywords del texto de la pregunta. Es una heurística, no una taxonomía
oficial: el objetivo es ver si el ~45% de win rate global esconde una
categoría con edge real mezclada con otra que la está arrastrando para
abajo, no clasificar con precisión perfecta cada mercado.

Uso:
    python analyze_polymarket_categories.py resultados_pm.csv
"""
import argparse
import csv
import re

CATEGORY_RULES = [
    # (nombre, patrón — se evalúa en orden, el primero que matchea gana)
    ("Lanzamientos / FDV",       re.compile(r"\bFDV\b|one day after launch|launch a token", re.I)),
    ("Valuaciones privadas",     re.compile(r"valuation hit|\(HIGH\)|\(LOW\)", re.I)),
    ("Objetivo de precio cripto", re.compile(r"\breach \$|\bdip to \$|market cap", re.I)),
    ("Política / elecciones",    re.compile(r"election|senat|vote|president|confirm|governor|congress|by-election", re.I)),
    ("Deportes / vanity",        re.compile(r"\bfight\b|attend|wedding|\bwin\b.*(match|game|fight)|UFC", re.I)),
    ("Macro / eventos",          re.compile(r"bank failure|hack over|hottest|record|open interest", re.I)),
    ("IA / tech",                re.compile(r"Claude|OpenAI|Anthropic|GPT|Frontier Math|Opus|Gemini", re.I)),
]


def categorize(question):
    for name, pattern in CATEGORY_RULES:
        if pattern.search(question):
            return name
    return "Otros / sin clasificar"


def load_rows(path):
    """
    Lee un CSV probando UTF-8 primero y cayendo a cp1252 si falla — los CSV
    generados con una versión de polymarket_backtest.py de antes de este fix
    pueden haber quedado en cp1252 (la codificación por defecto de Windows
    en inglés), y las preguntas de Polymarket suelen traer caracteres
    (guiones largos, comillas tipográficas) que no son UTF-8 válido en esa
    codificación.
    """
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))
    except UnicodeDecodeError:
        with open(path, newline="", encoding="cp1252") as f:
            return list(csv.DictReader(f))


def summarize(rows):
    n = len(rows)
    if n == 0:
        return {"n": 0}
    r_multiples = [float(r["r_multiple"]) for r in rows]
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
    parser.add_argument("csv_path")
    parser.add_argument("--min-n", type=int, default=5,
                         help="categorías con menos trades que esto se muestran pero marcadas como muestra insuficiente")
    args = parser.parse_args()

    rows = load_rows(args.csv_path)
    if not rows:
        print("CSV vacío.")
        return

    by_category = {}
    for r in rows:
        cat = categorize(r["question"])
        by_category.setdefault(cat, []).append(r)

    overall = summarize(rows)
    print(f"\n=== TOTAL: {overall['n']} trades | win rate {overall['win_rate']:.1f}% | "
          f"expectancy {overall['expectancy_r']:+.2f}R | profit factor {overall['profit_factor']:.2f} | "
          f"total {overall['total_r']:+.2f}R ===\n")

    ranked = sorted(by_category.items(), key=lambda kv: summarize(kv[1])["total_r"], reverse=True)
    print(f"{'Categoría':30s} {'n':>5s} {'Win%':>7s} {'Expect.R':>10s} {'PF':>6s} {'Total R':>9s}")
    print("-" * 72)
    for cat, cat_rows in ranked:
        s = summarize(cat_rows)
        flag = " ⚠️ muestra chica" if s["n"] < args.min_n else ""
        pf = f"{s['profit_factor']:.2f}" if s["profit_factor"] != float("inf") else "inf"
        print(f"{cat:30s} {s['n']:>5d} {s['win_rate']:>6.1f}% {s['expectancy_r']:>+9.2f}R {pf:>6s} {s['total_r']:>+8.2f}R{flag}")

    print("\nSi una o dos categorías concentran casi todo el total R positivo y el resto está en\n"
          "negativo o cerca de cero, la edge real del sistema probablemente está solo ahí adentro —\n"
          "vale la pena filtrar polymarket_main.py para avisar solo de esas categorías (agregando un\n"
          "chequeo de keywords sobre market['question'] antes de mandar el memo), en vez de escanear\n"
          "todos los mercados por igual.")


if __name__ == "__main__":
    main()
