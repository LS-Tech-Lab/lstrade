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


def format_days(days):
    """
    Convierte 'días hasta el cierre' a una unidad legible. Antes se
    mostraba siempre en días fijos ("0.1 días" para un mercado que
    cierra en un par de horas) — técnicamente correcto pero difícil de
    leer de un vistazo cuando el mercado cierra pronto.
    """
    if days is None:
        return "—"
    if days >= 1:
        return f"{days:.1f} días"
    hours = days * 24
    if hours >= 1:
        return f"{hours:.1f} horas"
    return f"{round(hours * 60)} minutos"


def format_pct(value):
    """'+3.8%' / '-2.1%' — con signo siempre visible."""
    return f"{value:+.1f}%"


def build_crypto_memo(symbol, signal, plan, deadline_seconds=None, markdown=True,
                       header=None, footer=None):
    """
    Memo de Telegram para una señal de cripto que ya pasó el filtro de
    riesgo (por eso no repite los checks acá: en este punto todos dieron
    OK — el detalle completo sigue disponible en el dashboard).

    AUDITORÍA (11/09/2026): rediseño de formato para más claridad —
    Entrada/Target/Stop pasan de una sola oración corrida a líneas
    propias con emoji distinto cada una (escaneable de un vistazo en
    el celular), se agrega el % de distancia de cada nivel respecto a
    la entrada (el precio solo no dice si el movimiento es chico o
    grande), se acorta "Si arriesgás X podés ganar hasta Y" al formato
    estándar riesgo/beneficio, y el tamaño de posición ahora también
    muestra el valor en $ además de las unidades (0.5589 SOL solo no
    dice cuánto capital hay en juego si no sabés el precio de memoria).
    Además admite pasar header/footer para que main.py/app.py no
    dupliquen ese texto por fuera de la función (antes "Posición
    abierta (papel)" y el aviso de modo papel vivían afuera, repetidos
    en las dos copias serverless/local).
    """
    direction = signal["direction"]
    is_long = direction == "LONG"
    action = "COMPRAR" if is_long else "VENDER"
    confidence = signal.get("confidence", 0)
    stars = "★" * confidence + "☆" * max(0, 5 - confidence)

    lines = []
    if header:
        lines.append(f"📈 *{header}* — {symbol}" if markdown else f"📈 {header} — {symbol}")
        lines.append("")

    label = f"SEÑAL: {action}"
    lines.append(f"🟢 *{label}*" if markdown else f"🟢 {label}")
    lines.append(f"Confianza: {stars} ({confidence}/5)")
    lines.append("")

    if plan:
        entry = plan["entry"]
        stop = plan["stop"]
        target = plan["target"]
        risk_amount = plan["risk_amount"]
        potential_gain = risk_amount * plan["rr"]

        target_pct = (target - entry) / entry * 100 if is_long else (entry - target) / entry * 100
        stop_pct = (stop - entry) / entry * 100 if is_long else (entry - stop) / entry * 100

        # 2 decimales fijos acá (a diferencia de format_money, que usa 4
        # para precios >=$1): en este memo prioriza legibilidad rápida
        # sobre precisión completa — $107.9848 vs $107.99 no cambia la
        # decisión de comprar/vender, y sí cambia cuánto cuesta leerlo.
        lines.append(f"💰 Entrada: ${entry:,.2f}")
        lines.append(f"🎯 Ganancia en: ${target:,.2f} ({format_pct(target_pct)})")
        lines.append(f"🛑 Pérdida en: ${stop:,.2f} ({format_pct(stop_pct)})")
        lines.append("")
        lines.append(
            f"📊 Riesgo/Beneficio: arriesgás ${risk_amount:,.2f} para ganar hasta "
            f"${potential_gain:,.2f} ({plan['rr']:.1f}x)"
        )
        position_value = plan["position_size"] * entry
        lines.append(
            f"📦 Tamaño: {plan['position_size']:.4f} {symbol.split('/')[0]} "
            f"(~${position_value:,.2f} al precio de entrada)"
        )
    else:
        lines.append(f"Precio actual: {format_money(signal.get('price'))}")

    if deadline_seconds:
        lines.append("")
        deadline_txt = format_duration_minutes(deadline_seconds)
        lines.append(f"⏱ Tenés {deadline_txt} para responder — si no contestás, se rechaza sola.")

    if footer:
        lines.append("")
        lines.append(f"_{footer}_" if markdown else footer)

    return "\n".join(lines)
