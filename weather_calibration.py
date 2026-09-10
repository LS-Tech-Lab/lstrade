"""
Analisis de calibracion + recalibracion para el modulo clima de lstrade.

Fuente: tabla weather_signals, snapshot tomado 2026-09-10.
38 senales con outcome yes/no (se excluyen las 32 con outcome='stop',
que son posiciones cerradas por stop-loss, no resoluciones de mercado).

Que hace este script:
1. Reconstruye la tabla de calibracion por bucket (para verificar contra
   la que ya tenias).
2. Ajusta DOS calibraciones candidatas sobre logit(my_prob):
   a) Shift-only (1 parametro): preserva el ranking exacto, solo corrige
      el nivel. Mas robusto con n=38 y 3 positivos.
   b) Platt scaling completo (2 parametros: pendiente + shift).
3. Evalua ambas con Leave-One-Out Cross-Validation (LOOCV) en vez de
   ajuste in-sample, porque con solo 3 positivos el in-sample fit
   engana facil (podria "memorizar" los 3 yes).
4. Imprime la tabla de calibracion recalculada con el modelo elegido.
5. Exporta la funcion final lista para pegar en weather_signal_engine.py.

Como refrescar los datos: correr el SELECT de abajo en el SQL editor de
Supabase y reemplazar la lista RAW_DATA.

    SELECT my_prob, outcome FROM weather_signals
    WHERE outcome IN ('yes','no') ORDER BY ts_resolved;
"""

import math

# ---------------------------------------------------------------------------
# 1. DATOS CRUDOS (snapshot 2026-09-10, 38 senales resueltas yes/no)
# ---------------------------------------------------------------------------
RAW_DATA = [
    (0.267822885480318, "no"), (0.394350226333145, "no"), (0.235589167283439, "no"),
    (0.199667556737955, "no"), (0.173660692366032, "no"), (0.440051071751467, "no"),
    (0.455382813820213, "no"), (0.0724559756370783, "no"), (0.394350226333145, "yes"),
    (0.0994401083410792, "no"), (0.287134578982592, "no"), (0.292595260657126, "no"),
    (0.154538028690828, "no"), (0.279252040918703, "no"), (0.323077760978621, "no"),
    (0.297671619036357, "no"), (0.232811345843834, "yes"), (0.0870393484769689, "no"),
    (0.193611185747203, "no"), (0.193611185747203, "yes"), (0.193611185747203, "no"),
    (0.209458355997115, "no"), (0.253064926812116, "no"), (0.316530117771822, "no"),
    (0.268701228997725, "no"), (0.169954597767105, "no"), (0.193611185747203, "no"),
    (0.115705215627924, "no"), (0.290152204929955, "no"), (0.297671619036357, "no"),
    (0.2245204905929, "no"), (0.231476274522286, "no"), (0.23532753338813, "no"),
    (0.145207227233956, "no"), (0.142372331125174, "no"), (0.173807162780929, "no"),
    (0.119567223000455, "no"), (0.107221872860331, "no"),
]

probs = [p for p, _ in RAW_DATA]
outcomes = [1 if o == "yes" else 0 for _, o in RAW_DATA]
n = len(RAW_DATA)


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


# ---------------------------------------------------------------------------
# 2. TABLA DE CALIBRACION POR BUCKET (verificacion contra la que ya tenias)
# ---------------------------------------------------------------------------
def bucket_table(probs, outcomes, edges=(0, .1, .2, .3, .4, .5, 1.01)):
    print(f"{'Bucket':<10}{'N':<5}{'Modelo dijo':<14}{'Paso de verdad':<16}")
    for lo, hi in zip(edges[:-1], edges[1:]):
        idx = [i for i, p in enumerate(probs) if lo <= p < hi]
        if not idx:
            continue
        said = sum(probs[i] for i in idx) / len(idx)
        real = sum(outcomes[i] for i in idx) / len(idx)
        print(f"{lo:.0%}-{hi if hi<1 else 1:.0%}   {len(idx):<5}{said:<14.1%}{real:<16.1%}")


print("=" * 60)
print("CALIBRACION CRUDA (sin corregir)")
print("=" * 60)
bucket_table(probs, outcomes)
print(f"\nProb. promedio del modelo: {sum(probs)/n:.1%}")
print(f"Tasa real de 'yes':        {sum(outcomes)/n:.1%}")
print(f"Log loss (in-sample):      {log_loss(outcomes, probs):.4f}")
print(f"Brier score (in-sample):   {brier(outcomes, probs):.4f}")


# ---------------------------------------------------------------------------
# 3. CALIBRACION A: SHIFT-ONLY EN ESPACIO LOGIT (1 parametro, c)
#    calibrated = sigmoid(logit(raw) + c)
#    Preserva el ranking exacto (es una transformacion monotona).
# ---------------------------------------------------------------------------
def fit_shift(probs, outcomes, iters=200, lr=0.5):
    c = 0.0
    logits = [logit(p) for p in probs]
    for _ in range(iters):
        grad = 0.0
        for lg, y in zip(logits, outcomes):
            pred = sigmoid(lg + c)
            grad += (pred - y)
        c -= lr * grad / n
    return c


def fit_platt(probs, outcomes, iters=500, lr=0.3):
    a, b = 1.0, 0.0  # a = pendiente, b = shift
    logits = [logit(p) for p in probs]
    for _ in range(iters):
        grad_a = grad_b = 0.0
        for lg, y in zip(logits, outcomes):
            pred = sigmoid(a * lg + b)
            err = pred - y
            grad_a += err * lg
            grad_b += err
        a -= lr * grad_a / n
        b -= lr * grad_b / n
    return a, b


def loocv_eval(fit_fn, probs, outcomes, apply_fn):
    """Deja-uno-fuera: ajusta con 37, predice el que queda, repite 38 veces."""
    preds = []
    for i in range(n):
        train_p = probs[:i] + probs[i + 1:]
        train_y = outcomes[:i] + outcomes[i + 1:]
        params = fit_fn(train_p, train_y)
        preds.append(apply_fn(probs[i], params))
    return preds


print("\n" + "=" * 60)
print("CANDIDATO A: shift-only (1 parametro, preserva ranking)")
print("=" * 60)
c_full = fit_shift(probs, outcomes)
shift_preds_full = [sigmoid(logit(p) + c_full) for p in probs]
print(f"Parametro c (ajustado con las 38): {c_full:+.3f}")
print(f"Log loss in-sample:  {log_loss(outcomes, shift_preds_full):.4f}")

loocv_shift_preds = loocv_eval(
    fit_shift, probs, outcomes, lambda p, c: sigmoid(logit(p) + c)
)
print(f"Log loss LOOCV:      {log_loss(outcomes, loocv_shift_preds):.4f}  <- comparar con esto")
print(f"Brier LOOCV:         {brier(outcomes, loocv_shift_preds):.4f}")

print("\n" + "=" * 60)
print("CANDIDATO B: Platt scaling completo (2 parametros)")
print("=" * 60)
a_full, b_full = fit_platt(probs, outcomes)
platt_preds_full = [sigmoid(a_full * logit(p) + b_full) for p in probs]
print(f"Parametros (a,b) ajustados con las 38: a={a_full:.3f}, b={b_full:+.3f}")
print(f"Log loss in-sample:  {log_loss(outcomes, platt_preds_full):.4f}")

loocv_platt_preds = loocv_eval(
    fit_platt, probs, outcomes, lambda p, ab: sigmoid(ab[0] * logit(p) + ab[1])
)
print(f"Log loss LOOCV:      {log_loss(outcomes, loocv_platt_preds):.4f}  <- comparar con esto")
print(f"Brier LOOCV:         {brier(outcomes, loocv_platt_preds):.4f}")

print("\n" + "=" * 60)
print("BASELINE: sin calibrar, para referencia")
print("=" * 60)
print(f"Log loss (todo in-sample, sesgado a favor): {log_loss(outcomes, probs):.4f}")

# ---------------------------------------------------------------------------
# 4. TABLA RECALIBRADA CON EL MODELO GANADOR (shift-only)
# ---------------------------------------------------------------------------
print("\n" + "=" * 60)
print(f"TABLA RECALIBRADA (shift-only, c={c_full:+.3f})")
print("=" * 60)
bucket_table(shift_preds_full, outcomes)

print("\n" + "=" * 60)
print("VEREDICTO")
print("=" * 60)
if log_loss(outcomes, loocv_shift_preds) < log_loss(outcomes, loocv_platt_preds):
    print("-> shift-only gana en LOOCV: usar solo el parametro c.")
    print(f"-> c = {c_full:+.4f}")
else:
    print("-> Platt completo gana en LOOCV, pero con n=38 y 3 positivos")
    print("   revisa si no es sobreajuste antes de confiar en 'a'.")
