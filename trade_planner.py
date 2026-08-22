"""
Arma el plan de trading (entrada, stop, objetivo) a partir de la señal y el
reporte de riesgo ya calculados. No decide nada por su cuenta: solo traduce
números en un plan ejecutable.
"""


def compute_plan(signal, risk_report, config):
    direction = signal["direction"]
    entry = signal["price"]
    stop_distance = risk_report["stop_distance"]
    rr = config.MIN_RR

    if direction == "LONG":
        stop = entry - stop_distance
        target = entry + stop_distance * rr
    else:
        stop = entry + stop_distance
        target = entry - stop_distance * rr

    return {
        "direction": direction,
        "entry": entry,
        "stop": stop,
        "target": target,
        "rr": rr,
        "position_size": risk_report["position_size"],
        "risk_amount": risk_report["risk_amount"],
    }
