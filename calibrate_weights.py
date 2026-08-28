"""
Calibra los pesos del score de signal_engine.py contra resultados reales,
en vez de usar las constantes actuales (elegidas a mano: momentum*1.2 +
trend_align*0.8 - volatility*1.5).

Usa una regresión logística simple (sin dependencias externas de ML — solo
gradiente descendente en Python puro) sobre el CSV que genera
`backtest.py --output resultados.csv --simulate-trailing`, para responder:
de las features que ya calcula generate_signal() (momentum, trend_align,
volatility, rsi, volume_ratio), ¿cuáles predicen de verdad que el trade
termine en "target" y no en "stop"?

Esto NO reemplaza automáticamente los pesos en signal_engine.py — imprime
los coeficientes y te deja decidir si aplicarlos, porque un cambio de score
hay que re-backtestear después de aplicarlo (los coeficientes de hoy son
válidos para el período que analizaste, no una verdad universal).

Uso:
    python backtest.py --days 365 --simulate-trailing --output resultados.csv
    python calibrate_weights.py resultados.csv
"""
import argparse
import csv
import math

FEATURES = ["momentum", "trend_align", "volatility", "rsi", "volume_ratio"]


def _read_csv_rows(path):
    """
    Lee un CSV probando UTF-8 primero y cayendo a cp1252 si falla — los CSV
    generados con una versión de backtest.py/polymarket_backtest.py de antes
    de este fix pueden haber quedado en cp1252 (la codificación por defecto
    de Windows en inglés), y las preguntas de Polymarket suelen traer
    caracteres (guiones largos, comillas tipográficas) que no son UTF-8
    válido en esa codificación.
    """
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))
    except UnicodeDecodeError:
        with open(path, newline="", encoding="cp1252") as f:
            return list(csv.DictReader(f))


def load_trades(path):
    rows = []
    for r in _read_csv_rows(path):
        if not r.get("outcome"):
            continue
        try:
            x = [abs(float(r[feat])) if feat != "rsi" else float(r[feat]) for feat in FEATURES]
        except (KeyError, ValueError):
            continue
        y = 1.0 if r["outcome"] == "target" else 0.0
        rows.append((x, y))
    return rows


def zscore_normalize(X):
    n = len(X)
    dims = len(X[0])
    means = [sum(row[j] for row in X) / n for j in range(dims)]
    stds = []
    for j in range(dims):
        var = sum((row[j] - means[j]) ** 2 for row in X) / n
        stds.append(math.sqrt(var) or 1.0)
    normed = [[(row[j] - means[j]) / stds[j] for j in range(dims)] for row in X]
    return normed, means, stds


def sigmoid(z):
    if z < -60:
        return 0.0
    if z > 60:
        return 1.0
    return 1.0 / (1.0 + math.exp(-z))


def train_logistic(X, y, epochs=2000, lr=0.1, l2=0.001):
    n = len(X)
    dims = len(X[0])
    weights = [0.0] * dims
    bias = 0.0

    for _ in range(epochs):
        grad_w = [0.0] * dims
        grad_b = 0.0
        for xi, yi in zip(X, y):
            z = bias + sum(w * x for w, x in zip(weights, xi))
            pred = sigmoid(z)
            err = pred - yi
            for j in range(dims):
                grad_w[j] += err * xi[j]
            grad_b += err
        for j in range(dims):
            weights[j] -= lr * (grad_w[j] / n + l2 * weights[j])
        bias -= lr * (grad_b / n)

    return weights, bias


def evaluate(X, y, weights, bias, threshold=0.5):
    correct = 0
    for xi, yi in zip(X, y):
        z = bias + sum(w * x for w, x in zip(weights, xi))
        pred = 1.0 if sigmoid(z) >= threshold else 0.0
        if pred == yi:
            correct += 1
    return correct / len(y) if y else 0.0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_path", help="CSV generado por backtest.py --output")
    parser.add_argument("--test-frac", type=float, default=0.25,
                         help="fracción del final del archivo usada como test (no se entrena con ella)")
    args = parser.parse_args()

    rows = load_trades(args.csv_path)
    if len(rows) < 30:
        print(f"Solo {len(rows)} trades con resultado en el CSV — muy pocos para calibrar nada útil. "
              f"Necesitás más historial (más símbolos y/o --days más grande en el backtest).")
        return

    split = int(len(rows) * (1 - args.test_frac))
    train_rows, test_rows = rows[:split], rows[split:]

    X_train_raw = [r[0] for r in train_rows]
    y_train = [r[1] for r in train_rows]
    X_test_raw = [r[0] for r in test_rows]
    y_test = [r[1] for r in test_rows]

    X_train, means, stds = zscore_normalize(X_train_raw)
    X_test = [[(x[j] - means[j]) / stds[j] for j in range(len(FEATURES))] for x in X_test_raw]

    weights, bias = train_logistic(X_train, y_train)

    base_rate = sum(y_train) / len(y_train)
    train_acc = evaluate(X_train, y_train, weights, bias)
    test_acc = evaluate(X_test, y_test, weights, bias) if X_test else None

    print(f"\n=== Calibración de pesos sobre {len(rows)} trades ({len(train_rows)} train / {len(test_rows)} test) ===")
    print(f"Win rate base (siempre predecir 'target'): {base_rate*100:.1f}%")
    print(f"Accuracy del modelo en train: {train_acc*100:.1f}%")
    if test_acc is not None:
        print(f"Accuracy del modelo en test (out-of-sample): {test_acc*100:.1f}%")
        if test_acc < base_rate + 0.02:
            print("⚠️  El modelo apenas mejora (o no mejora) al predecir siempre la clase mayoritaria en "
                  "test — con este volumen de datos, las features actuales no muestran una señal predictiva "
                  "clara todavía. No conviene reemplazar los pesos actuales con esto sin más historial.")

    print("\nCoeficientes (en unidades estandarizadas — el signo indica dirección de la relación,\n"
          "la magnitud relativa indica importancia; NO son pesos directos para pegar en signal_engine.py):")
    for feat, w in zip(FEATURES, weights):
        print(f"  {feat:15s}: {w:+.4f}")
    print(f"  {'bias':15s}: {bias:+.4f}")

    print("\nPara usarlos en signal_engine.py: normalizá cada feature con su media/desvío del período "
          "analizado (quedan abajo) antes de aplicar estos coeficientes, o simplemente usá el signo y el "
          "orden de magnitud relativo para reponderar los términos actuales del score a mano.")
    print("\nMedias / desvíos usados para normalizar (necesarios si reusás los coeficientes tal cual):")
    for feat, m, s in zip(FEATURES, means, stds):
        print(f"  {feat:15s}: media={m:.5f}  desvío={s:.5f}")


if __name__ == "__main__":
    main()
