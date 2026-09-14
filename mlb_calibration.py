"""
Ajuste de calibración para el módulo MLB de lstrade -- reemplazo del clip
duro [MLB_PROB_CLIP_MIN, MLB_PROB_CLIP_MAX] = [0.40, 0.60] por una curva de
calibración real fiteada sobre el backtest histórico (mismo pedido del
usuario del 13/09/2026: "el clip actual le quita al modelo toda su
capacidad de diferenciar entre un 55% y un 58%").

Mismo enfoque que weather_calibration.py (logit-space, shift-only vs. Platt
scaling completo), con una diferencia deliberada: weather_calibration.py
usa LOOCV porque tenía n=38 con solo 3 positivos (en una muestra tan chica,
un ajuste in-sample "memoriza" los pocos positivos). Acá el dataset de
backtest_mlb.py tiene miles de partidos -- LOOCV sería carísimo y
innecesario. En su lugar se usa un split CRONOLÓGICO train/holdout (el
holdout es el período más reciente): fitear y evaluar en el mismo período
sobreestimaría qué tan bien generaliza la curva a partidos futuros, que es
exactamente para lo que se la quiere usar.

USO:
    python mlb_calibration.py --input resultados_mlb.csv
    python mlb_calibration.py --input resultados_mlb.csv --holdout-frac 0.25 --prob-col raw_prob_home

Fuente de --input: el CSV que genera backtest_mlb.py con --output (ver ese
script y su workflow .github/workflows/backtest-mlb.yml).

IMPORTANTE -- correr esto DESPUÉS de un backtest fresco: si mlb_signal_engine.py
cambió desde la última corrida de backtest_mlb.py (por ejemplo, el cambio del
13/09/2026 que movió HOME_FIELD_EDGE a espacio de log-odds y agregó
MIN_GAMES_FOR_FORM), un CSV viejo tiene raw_prob_home calculado con la
matemática VIEJA -- la curva que salga de acá calibraría el modelo que ya no
existe. Revisar MODEL_VERSION en el CSV/en los logs de esa corrida (no viene
en el CSV de backtest_mlb.py todavía; si hace falta, se puede agregar una
columna model_version a ese CSV el día que se quiera automatizar esta
verificación) contra mlb_signal_engine.MODEL_VERSION actual antes de confiar
en los parámetros de salida.
"""
import argparse
import csv
import math

MLB_STATS_API = None  # no se usa acá, solo para dejar claro que este script no pega a ninguna API


def logit(p, eps=1e-6):
    p = min(max(p, eps), 1 - eps)
    return math.log(p / (1 - p))


def sigmoid(x):
    return 1 / (1 + math.exp(-x))


def log_loss(y_true, y_pred, eps=1e-9):
    total = 0.0
    for y, p in zip(y_true, y_pred):
        p = min(max(p, eps), 1 - eps)
        total += -(y * math.log(p) + (1 - y) * math.log(1 - p))
    return total / len(y_true)


def brier(y_true, y_pred):
    return sum((y - p) ** 2 for y, p in zip(y_true, y_pred)) / len(y_true)


def load_data(path, prob_col):
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            if not r.get(prob_col) or r.get("home_won") not in ("True", "False"):
                continue
            rows.append({
                "date": r["date"],
                "prob": float(r[prob_col]),
                "outcome": 1 if r["home_won"] == "True" else 0,
            })
    rows.sort(key=lambda r: r["date"])
    return rows


def bucket_table(probs, outcomes, edges=(0, .1, .2, .3, .4, .5, .6, .7, .8, .9, 1.01)):
    print(f"  {'Bucket':<10}{'N':<7}{'Predicho':<12}{'Real':<10}")
    for lo, hi in zip(edges[:-1], edges[1:]):
        idx = [i for i, p in enumerate(probs) if lo <= p < hi]
        if not idx:
            continue
        said = sum(probs[i] for i in idx) / len(idx)
        real = sum(outcomes[i] for i in idx) / len(idx)
        print(f"  {lo:.0%}-{min(hi,1):.0%}      {len(idx):<7}{said:<12.1%}{real:<10.1%}")


# ---------------------------------------------------------------------------
# Shift-only (1 parámetro, preserva el ranking exacto -- calibrated =
# sigmoid(logit(raw) + c)) y Platt completo (2 parámetros: pendiente + shift)
# -- misma implementación que weather_calibration.py, sin dependencias
# externas (nada de sklearn) para no agregarle una dependencia nueva al
# entorno de producción solo por esto.
# ---------------------------------------------------------------------------

def fit_shift(probs, outcomes, iters=300, lr=0.5):
    c = 0.0
    logits = [logit(p) for p in probs]
    n = len(probs)
    for _ in range(iters):
        grad = sum(sigmoid(lg + c) - y for lg, y in zip(logits, outcomes))
        c -= lr * grad / n
    return c


def fit_platt(probs, outcomes, iters=800, lr=0.3):
    a, b = 1.0, 0.0
    logits = [logit(p) for p in probs]
    n = len(probs)
    for _ in range(iters):
        grad_a = grad_b = 0.0
        for lg, y in zip(logits, outcomes):
            err = sigmoid(a * lg + b) - y
            grad_a += err * lg
            grad_b += err
        a -= lr * grad_a / n
        b -= lr * grad_b / n
    return a, b


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="CSV de salida de backtest_mlb.py (--output)")
    parser.add_argument("--prob-col", default="raw_prob_home",
                         help="columna a calibrar -- raw_prob_home (sin el cap 0.05-0.95 de sanidad, "
                              "recomendado) o prob_home (con ese cap ya aplicado)")
    parser.add_argument("--holdout-frac", type=float, default=0.2,
                         help="fracción final CRONOLÓGICA (0-1) usada como holdout -- no se toca al fitear")
    args = parser.parse_args()

    data = load_data(args.input, args.prob_col)
    n = len(data)
    if n < 100:
        print(f"ADVERTENCIA: solo {n} filas con datos completos -- muy poco para fitear con confianza.")

    cutoff = int(n * (1 - args.holdout_frac))
    train, holdout = data[:cutoff], data[cutoff:]
    print(f"Total: {n} partidos | train: {len(train)} (hasta {train[-1]['date']}) | "
          f"holdout: {len(holdout)} (desde {holdout[0]['date']})")

    train_p = [r["prob"] for r in train]
    train_y = [r["outcome"] for r in train]
    hold_p = [r["prob"] for r in holdout]
    hold_y = [r["outcome"] for r in holdout]

    print("\n" + "=" * 60)
    print(f"CALIBRACIÓN CRUDA -- columna '{args.prob_col}', holdout sin tocar")
    print("=" * 60)
    bucket_table(hold_p, hold_y)
    print(f"  Brier holdout (sin calibrar):    {brier(hold_y, hold_p):.4f}")
    print(f"  Log loss holdout (sin calibrar): {log_loss(hold_y, hold_p):.4f}")

    print("\n" + "=" * 60)
    print("CANDIDATO A: shift-only (1 parámetro, preserva ranking) -- fiteado solo con train")
    print("=" * 60)
    c = fit_shift(train_p, train_y)
    shift_hold_preds = [sigmoid(logit(p) + c) for p in hold_p]
    print(f"  Parámetro c: {c:+.4f}")
    print(f"  Brier holdout:    {brier(hold_y, shift_hold_preds):.4f}")
    print(f"  Log loss holdout: {log_loss(hold_y, shift_hold_preds):.4f}  <- comparar con crudo arriba")
    bucket_table(shift_hold_preds, hold_y)

    print("\n" + "=" * 60)
    print("CANDIDATO B: Platt scaling completo (2 parámetros) -- fiteado solo con train")
    print("=" * 60)
    a, b = fit_platt(train_p, train_y)
    platt_hold_preds = [sigmoid(a * logit(p) + b) for p in hold_p]
    print(f"  Parámetros: a={a:.4f} (pendiente), b={b:+.4f} (shift)")
    print(f"  Brier holdout:    {brier(hold_y, platt_hold_preds):.4f}")
    print(f"  Log loss holdout: {log_loss(hold_y, platt_hold_preds):.4f}  <- comparar con crudo arriba")
    bucket_table(platt_hold_preds, hold_y)

    print("\n" + "=" * 60)
    print("VEREDICTO (evaluado en holdout, no en train)")
    print("=" * 60)
    shift_ll = log_loss(hold_y, shift_hold_preds)
    platt_ll = log_loss(hold_y, platt_hold_preds)
    raw_ll = log_loss(hold_y, hold_p)
    if min(shift_ll, platt_ll) >= raw_ll:
        print("-> Ninguna calibración mejora sobre el crudo en el holdout -- revisar antes de")
        print("   reemplazar el clip actual. Puede ser señal de que hace falta más historia,")
        print("   o de que el problema real está en los componentes del modelo, no en la calibración.")
        return

    if shift_ll <= platt_ll:
        print(f"-> shift-only gana en holdout: c = {c:+.4f}")
        print("\n   Función lista para pegar en mlb_signal_engine.py (reemplaza el clip fijo):\n")
        print(f"   def calibrate_prob(raw_prob, c={c:.4f}):")
        print("       \"\"\"Calibración fiteada sobre backtest histórico -- ver mlb_calibration.py.")
        print("       Reemplaza a Config.MLB_PROB_CLIP_MIN/MAX.\"\"\"")
        print("       return 1.0 / (1.0 + math.exp(-(to_log_odds(raw_prob) + c)))")
    else:
        print(f"-> Platt completo gana en holdout: a={a:.4f}, b={b:+.4f}")
        print("   (revisar que no sea sobreajuste si el holdout es chico)")
        print("\n   Función lista para pegar en mlb_signal_engine.py (reemplaza el clip fijo):\n")
        print(f"   def calibrate_prob(raw_prob, a={a:.4f}, b={b:.4f}):")
        print("       \"\"\"Calibración fiteada sobre backtest histórico -- ver mlb_calibration.py.")
        print("       Reemplaza a Config.MLB_PROB_CLIP_MIN/MAX.\"\"\"")
        print("       return 1.0 / (1.0 + math.exp(-(a * to_log_odds(raw_prob) + b)))")


if __name__ == "__main__":
    main()
