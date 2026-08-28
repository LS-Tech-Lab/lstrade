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

    plt.rcParams["font.family"] = "sans-serif"
    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=140)
    fig.patch.set_facecolor("#FFFFFF")

    # Área bajo la curva, para dar peso visual a la trayectoria del precio
    ax.fill_between(xs, prices, 0, color="#2E86DE", alpha=0.06, zorder=1)
    ax.plot(xs, prices, color="#2E86DE", linewidth=2.2, label="Precio YES", zorder=3)

    # Último precio: punto marcado + valor anotado, para que el nivel actual
    # se lea de un vistazo sin tener que ubicarlo en el eje
    last_x, last_p = xs[-1], prices[-1]
    ax.scatter([last_x], [last_p], color="#181410", s=32, zorder=4)
    ax.annotate(
        f"{last_p:.3f}",
        xy=(last_x, last_p),
        xytext=(8, 0),
        textcoords="offset points",
        va="center",
        fontsize=9,
        fontweight="bold",
    )

    tp = signal.get("trade_plan")
    direction = signal["direction"]
    if tp:
        entry_display = tp["entry"] if direction == "YES" else 1 - tp["entry"]
        target_display = tp["target"] if direction == "YES" else 1 - tp["target"]
        stop_display = tp["stop"] if direction == "YES" else 1 - tp["stop"]

        # Zonas sombreadas de ganancia (entrada→target) y pérdida (entrada→stop),
        # en vez de solo líneas punteadas — de un vistazo se ve cuánto recorrido
        # falta para cada escenario, no solo dónde están los niveles
        ax.axhspan(min(entry_display, target_display), max(entry_display, target_display),
                   color="#27AE60", alpha=0.10, zorder=0)
        ax.axhspan(min(entry_display, stop_display), max(entry_display, stop_display),
                   color="#C0392B", alpha=0.10, zorder=0)

        ax.axhline(entry_display, color="#7F8C8D", linestyle="--", linewidth=1.2, label=f"Entrada {tp['entry']:.3f}", zorder=2)
        ax.axhline(target_display, color="#27AE60", linestyle="--", linewidth=1.2, label=f"Target {tp['target']:.3f}", zorder=2)
        ax.axhline(stop_display, color="#C0392B", linestyle="--", linewidth=1.2, label=f"Stop {tp['stop']:.3f}", zorder=2)

    question = signal["market"]["question"]
    title = question[:70] + ("…" if len(question) > 70 else "")
    dir_color = "#27AE60" if direction == "YES" else "#C0392B"
    ax.set_title(f"{title}", fontsize=11, fontweight="bold", pad=28)
    ax.text(0.0, 1.05, f"Señal: {direction} · confianza {signal['confidence']}/5",
            transform=ax.transAxes, fontsize=9.5, color=dir_color, fontweight="bold")
    ax.set_ylabel("Precio YES (prob. implícita)")
    ax.set_xlabel("Períodos recientes")
    ax.set_ylim(0, 1)
    ax.legend(loc="upper left", bbox_to_anchor=(1.01, 1.0), fontsize=8, frameon=False)
    ax.grid(alpha=0.2)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()