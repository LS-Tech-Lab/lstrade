"""
Gráfico de la curva de probabilidad (precio YES) de un mercado de Polymarket,
con el plan de salida sugerido (entrada/target/stop) marcado — para mandar
como imagen junto al memo de texto en Telegram. Antes, la señal de Polymarket
era pura data en texto a pesar de tener un historial de precios ideal para
visualizar de un vistazo hacia dónde viene moviéndose el mercado.

Se usa matplotlib con backend "Agg" (sin display) para poder correr en un
servidor/VPS sin entorno gráfico.
"""
import io

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


def build_signal_chart(signal, price_history):
    """
    signal: el dict que devuelve generate_polymarket_signal()
    price_history: la lista de puntos {"t":..., "p":...} usada para esa señal
    Devuelve bytes de un PNG, listos para TelegramNotifier.send_photo().
    """
    prices = [p["p"] for p in price_history]
    xs = list(range(len(prices)))

    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=120)
    ax.plot(xs, prices, color="#2E86DE", linewidth=2, label="Precio YES")

    tp = signal.get("trade_plan")
    direction = signal["direction"]
    if tp:
        entry_display = tp["entry"] if direction == "YES" else 1 - tp["entry"]
        target_display = tp["target"] if direction == "YES" else 1 - tp["target"]
        stop_display = tp["stop"] if direction == "YES" else 1 - tp["stop"]

        ax.axhline(entry_display, color="#7F8C8D", linestyle="--", linewidth=1, label=f"Entrada {tp['entry']:.3f}")
        ax.axhline(target_display, color="#27AE60", linestyle="--", linewidth=1, label=f"Target {tp['target']:.3f}")
        ax.axhline(stop_display, color="#C0392B", linestyle="--", linewidth=1, label=f"Stop {tp['stop']:.3f}")

    question = signal["market"]["question"]
    title = question[:70] + ("…" if len(question) > 70 else "")
    ax.set_title(f"{title}\nSeñal: {direction} · confianza {signal['confidence']}/5", fontsize=10)
    ax.set_ylabel("Precio YES (prob. implícita)")
    ax.set_xlabel("Períodos recientes")
    ax.set_ylim(0, 1)
    ax.legend(loc="best", fontsize=8)
    ax.grid(alpha=0.25)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)
    return buf.read()
