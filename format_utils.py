"""
Formato de números para mensajes de Telegram y logs — compartido entre
app.py, position_manager.py y risk_manager.py.

AUDITORÍA (06/09/2026): los mensajes de Cripto mostraban siempre 6
decimales fijos ("{price:.6f}"), sin importar el símbolo. Para una
altcoin de centavos eso tiene sentido (0.001234), pero para BTC/ETH da
"67234.891234" — precisión que nadie necesita y que hace más difícil
leer el número de un vistazo. format_price() adapta la cantidad de
decimales a la magnitud del precio, igual que ya hacía el dashboard
(toLocaleString con maximumFractionDigits) para los indicadores en vivo.
"""


def format_price(price):
    """
    Devuelve el precio formateado con la cantidad de decimales que tiene
    sentido según su magnitud, y separador de miles arriba de $1000.
    Sin decimales colgando en cero (67234.50 -> "67,234.5", no
    "67,234.500000").
    """
    if price is None:
        return "—"
    try:
        price = float(price)
    except (TypeError, ValueError):
        return "—"

    if price >= 1000:
        decimals = 2
    elif price >= 1:
        decimals = 4
    elif price >= 0.01:
        decimals = 6
    else:
        decimals = 8

    text = f"{price:,.{decimals}f}"
    # Recorta ceros finales sobrantes pero deja al menos 2 decimales
    # (para que $1 no quede como "$1" pelado, que se confunde con un
    # símbolo de acción en vez de un precio).
    if "." in text:
        integer_part, decimal_part = text.split(".")
        decimal_part = decimal_part.rstrip("0")
        if len(decimal_part) < 2:
            decimal_part = (decimal_part + "00")[:2]
        text = f"{integer_part}.{decimal_part}"
    return text


def format_money(price, symbol="$"):
    return f"{symbol}{format_price(price)}"


def direction_label(direction):
    """Traduce LONG/SHORT a Compra/Venta para mensajes en criollo."""
    return {"LONG": "Compra", "SHORT": "Venta", "YES": "SÍ", "NO": "NO"}.get(direction, direction or "—")


def format_duration_minutes(seconds):
    """Convierte segundos a un texto corto tipo '60 min' o '1h 30min'."""
    if seconds is None:
        return "—"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes // 60
    rem = minutes % 60
    return f"{hours}h {rem}min" if rem else f"{hours}h"


def build_crypto_memo(symbol, signal, plan, deadline_seconds=None, markdown=True):
    """
    Memo de Telegram para una señal de cripto que ya pasó el filtro de
    riesgo (por eso no repite los checks acá: en este punto todos dieron
    OK — el detalle completo sigue disponible en el dashboard). Mismo
    patrón que build_weather_memo() / build_mlb_memo(): primero la
    decisión en una frase ("COMPRAR"/"VENDER" + símbolo), después
    cuánto se puede ganar/perder en pesos y no solo en ratio, y por
    último — si se llama desde un memo de APROBACIÓN, no de "posición
    abierta en papel" — cuánto tiempo hay para responder.

    AUDITORÍA (06/09/2026): unifica lo que antes eran dos copias casi
    idénticas (app.py::build_memo_markdown y main.py::build_memo_text),
    las dos con el mismo problema: mostraban Entrada/Stop/Target como
    tres precios pelados con 6 decimales fijos y "Ratio R:B: 1:2.20"
    sin traducir — alguien sin experiencia en trading no tenía forma de
    saber, de un vistazo, si esto era pedirle aprobar una compra o una
    venta, ni cuánta plata real estaba en juego.
    """
    direction = signal["direction"]
    is_long = direction == "LONG"
    action = "COMPRAR" if is_long else "VENDER"
    confidence = signal.get("confidence", 0)
    stars = "★" * confidence + "☆" * max(0, 5 - confidence)

    label = f"SEÑAL: {action} {symbol}"
    lines = [f"🟢 *{label}*" if markdown else f"🟢 {label}"]
    lines.append(f"Confianza: {stars} ({confidence}/5)")
    lines.append("")

    if plan:
        risk_amount = plan["risk_amount"]
        potential_gain = risk_amount * plan["rr"]
        lines.append(
            f"Si arriesgás {format_money(risk_amount)}, podés ganar hasta "
            f"{format_money(potential_gain)} ({plan['rr']:.1f} veces lo arriesgado)"
        )
        entry_txt = format_money(plan["entry"])
        stop_txt = format_money(plan["stop"])
        target_txt = format_money(plan["target"])
        fall_word = "baja" if is_long else "sube"
        rise_word = "sube" if is_long else "baja"
        lines.append(
            f"Entrada: {entry_txt}  |  Si {fall_word} a {stop_txt} se cierra con pérdida  |  "
            f"Si {rise_word} a {target_txt} se cierra con ganancia"
        )
        lines.append(f"Tamaño de la posición: {plan['position_size']:.6f} unidades")
    else:
        lines.append(f"Precio actual: {format_money(signal.get('price'))}")

    if deadline_seconds:
        lines.append("")
        deadline_txt = format_duration_minutes(deadline_seconds)
        lines.append(f"⏱ Tenés {deadline_txt} para responder — si no contestás, se rechaza sola.")

    return "\n".join(lines)
