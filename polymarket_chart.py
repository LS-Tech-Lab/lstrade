"""
Gráfico de la curva de probabilidad (precio YES) de un mercado de Polymarket,
con el plan de salida sugerido (entrada/target/stop) marcado — para mandar
como imagen junto al memo de texto en Telegram. Antes, la señal de Polymarket
era pura data en texto a pesar de tener un historial de precios ideal para
visualizar de un vistazo hacia dónde viene moviéndose el mercado.

AUDITORÍA (22/09/2026): reescrito sin matplotlib para bajar el peso del
bundle de Vercel Functions. matplotlib arrastraba numpy, fontTools,
contourpy, kiwisolver y pillow como dependencias (~164 MB) solo para
dibujar una curva simple. Esta versión dibuja todo a mano con Pillow
(que ya venía como dependencia transitiva de matplotlib y ahora queda
como la única dependencia de este módulo), sobremuestreando 2x y
reduciendo al final para lograr líneas y texto suavizados sin necesitar
un motor de anti-aliasing dedicado.

La fuente embebida de Pillow (Aileron, cargada vía ImageFont.load_default)
NO tiene glifos para tildes/ñ/¿ del español -- se probó y renderiza
recuadros vacíos ("tofu"). Por eso se empaquetan aquí DejaVu Sans Regular
y Bold (assets/fonts/, ~1.5 MB en total, extraídas de los propios datos
de matplotlib antes de quitarlo -- licencia Bitstream Vera, ver
assets/fonts/LICENSE.txt) en vez de la fuente por defecto de Pillow.
1.5 MB es insignificante contra los ~164 MB que se ahorran al eliminar
matplotlib/numpy/fontTools/contourpy/kiwisolver del bundle.
"""
import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

_FONT_DIR = Path(__file__).parent / "assets" / "fonts"
_FONT_REGULAR = _FONT_DIR / "DejaVuSans.ttf"
_FONT_BOLD = _FONT_DIR / "DejaVuSans-Bold.ttf"

# Colores (mismos que la versión anterior con matplotlib)
_COLOR_LINE = (46, 134, 222)      # #2E86DE
_COLOR_LAST_POINT = (24, 20, 16)  # #181410
_COLOR_GREEN = (39, 174, 96)      # #27AE60
_COLOR_RED = (192, 57, 43)        # #C0392B
_COLOR_GRAY = (127, 140, 141)     # #7F8C8D
_COLOR_GRID = (0, 0, 0, 40)
_COLOR_AXIS = (60, 60, 60)

# Tamaño final del PNG (equivalente a los 8x4.5in @140dpi de antes)
_W, _H = 1120, 630
_SS = 2  # factor de sobremuestreo para suavizar líneas/curvas y texto
_MARGIN_L, _MARGIN_R, _MARGIN_T, _MARGIN_B = 70, 190, 78, 58


def _dashed_hline(draw, x0, x1, y, color, width, dash=9, gap=6):
    x = x0
    while x < x1:
        xe = min(x + dash, x1)
        draw.line([(x, y), (xe, y)], fill=color, width=width)
        x += dash + gap


def build_signal_chart(signal, price_history):
    """
    signal: el dict que devuelve generate_polymarket_signal()
    price_history: la lista de puntos {"t":..., "p":...} usada para esa señal
    Devuelve bytes de un PNG, listos para TelegramNotifier.send_photo().
    """
    prices = [p["p"] for p in price_history] or [0.0]
    n = len(prices)
    xs = list(range(n))

    ss = _SS
    W, H = _W * ss, _H * ss
    mL, mR, mT, mB = (_MARGIN_L * ss, _MARGIN_R * ss, _MARGIN_T * ss, _MARGIN_B * ss)
    plot_x0, plot_x1 = mL, W - mR
    plot_y0, plot_y1 = mT, H - mB  # y0 = arriba (precio 1.0), y1 = abajo (precio 0.0)

    def px(xi, yi):
        x_span = max(n - 1, 1)
        fx = plot_x0 + (xi / x_span) * (plot_x1 - plot_x0)
        fy = plot_y1 - max(0.0, min(1.0, yi)) * (plot_y1 - plot_y0)
        return (fx, fy)

    base = Image.new("RGBA", (W, H), (255, 255, 255, 255))
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)

    # Grid horizontal (0.0 a 1.0 cada 0.2)
    for i in range(6):
        level = i / 5.0
        _, y = px(0, level)
        odraw.line([(plot_x0, y), (plot_x1, y)], fill=_COLOR_GRID, width=max(1, ss))

    # Área bajo la curva (mismo alpha ~0.06 que antes)
    fill_alpha = int(0.06 * 255)
    poly = [px(xi, p) for xi, p in zip(xs, prices)]
    poly = [px(0, 0)] + poly + [px(n - 1, 0)]
    odraw.polygon(poly, fill=(*_COLOR_LINE, fill_alpha))

    tp = signal.get("trade_plan")
    direction = signal["direction"]
    entry_display = target_display = stop_display = None
    if tp:
        entry_display = tp["entry"] if direction == "YES" else 1 - tp["entry"]
        target_display = tp["target"] if direction == "YES" else 1 - tp["target"]
        stop_display = tp["stop"] if direction == "YES" else 1 - tp["stop"]

        # Zonas sombreadas ganancia (entrada→target) y pérdida (entrada→stop)
        _, y_entry = px(0, entry_display)
        _, y_target = px(0, target_display)
        _, y_stop = px(0, stop_display)
        band_alpha = int(0.10 * 255)
        odraw.rectangle(
            [plot_x0, min(y_entry, y_target), plot_x1, max(y_entry, y_target)],
            fill=(*_COLOR_GREEN, band_alpha),
        )
        odraw.rectangle(
            [plot_x0, min(y_entry, y_stop), plot_x1, max(y_entry, y_stop)],
            fill=(*_COLOR_RED, band_alpha),
        )

    base = Image.alpha_composite(base, overlay)
    draw = ImageDraw.Draw(base)

    font_title = ImageFont.truetype(str(_FONT_BOLD), size=15 * ss)
    font_subtitle = ImageFont.truetype(str(_FONT_BOLD), size=13 * ss)
    font_label = ImageFont.truetype(str(_FONT_REGULAR), size=12.5 * ss)
    font_tick = ImageFont.truetype(str(_FONT_REGULAR), size=11 * ss)
    font_annot = ImageFont.truetype(str(_FONT_BOLD), size=12 * ss)

    # Ejes (solo izquierdo e inferior, como spines top/right ocultos antes)
    draw.line([(plot_x0, plot_y0), (plot_x0, plot_y1)], fill=_COLOR_AXIS, width=ss)
    draw.line([(plot_x0, plot_y1), (plot_x1, plot_y1)], fill=_COLOR_AXIS, width=ss)

    # Etiquetas del eje Y
    for i in range(6):
        level = i / 5.0
        _, y = px(0, level)
        label = f"{level:.1f}"
        bbox = draw.textbbox((0, 0), label, font=font_tick)
        tw = bbox[2] - bbox[0]
        draw.text((plot_x0 - tw - 8 * ss, y - (bbox[3] - bbox[1]) / 2), label,
                   font=font_tick, fill=_COLOR_AXIS)

    # Líneas punteadas de entrada/target/stop
    if tp:
        for level, color, text in (
            (entry_display, _COLOR_GRAY, f"Entrada {tp['entry']:.3f}"),
            (target_display, _COLOR_GREEN, f"Target {tp['target']:.3f}"),
            (stop_display, _COLOR_RED, f"Stop {tp['stop']:.3f}"),
        ):
            _, y = px(0, level)
            _dashed_hline(draw, plot_x0, plot_x1, y, color, width=max(2, int(1.3 * ss)),
                          dash=9 * ss, gap=6 * ss)

    # Curva principal (encima de las bandas/punteadas)
    line_pts = [px(xi, p) for xi, p in zip(xs, prices)]
    if len(line_pts) >= 2:
        draw.line(line_pts, fill=_COLOR_LINE, width=max(2, int(2.2 * ss)), joint="curve")
    last_x, last_p = xs[-1], prices[-1]
    lx, ly = px(last_x, last_p)
    r = 3.5 * ss
    draw.ellipse([lx - r, ly - r, lx + r, ly + r], fill=_COLOR_LAST_POINT)
    draw.text((lx + 8 * ss, ly), f"{last_p:.3f}", font=font_annot, fill=_COLOR_LAST_POINT,
               anchor="lm")

    # Título (pregunta del mercado, truncada) + línea de señal
    question = signal["market"]["question"]
    title = question[:70] + ("…" if len(question) > 70 else "")
    draw.text((mL, 8 * ss), title, font=font_title, fill=(20, 20, 20))
    dir_color = _COLOR_GREEN if direction == "YES" else _COLOR_RED
    draw.text((mL, 8 * ss + 22 * ss), f"Señal: {direction} · confianza {signal['confidence']}/5",
               font=font_subtitle, fill=dir_color)

    # Etiquetas de ejes
    draw.text(((plot_x0 + plot_x1) / 2, H - 18 * ss), "Períodos recientes",
               font=font_label, fill=_COLOR_AXIS, anchor="mm")
    ylabel_img = Image.new("RGBA", (300 * ss, 24 * ss), (0, 0, 0, 0))
    yl_draw = ImageDraw.Draw(ylabel_img)
    yl_draw.text((0, 0), "Precio YES (prob. implícita)", font=font_label, fill=_COLOR_AXIS)
    ylabel_img = ylabel_img.rotate(90, expand=True)
    base.alpha_composite(ylabel_img, (int(14 * ss), int((plot_y0 + plot_y1) / 2 - ylabel_img.height / 2)))

    # Leyenda a la derecha
    legend_items = [(_COLOR_LINE, "Precio YES")]
    if tp:
        legend_items += [
            (_COLOR_GRAY, f"Entrada {tp['entry']:.3f}"),
            (_COLOR_GREEN, f"Target {tp['target']:.3f}"),
            (_COLOR_RED, f"Stop {tp['stop']:.3f}"),
        ]
    ly_cursor = plot_y0
    for color, text in legend_items:
        draw.line([(plot_x1 + 14 * ss, ly_cursor + 8 * ss), (plot_x1 + 34 * ss, ly_cursor + 8 * ss)],
                   fill=color, width=max(2, int(2 * ss)))
        draw.text((plot_x1 + 40 * ss, ly_cursor), text, font=font_tick, fill=(40, 40, 40))
        ly_cursor += 22 * ss

    final = base.convert("RGB").resize((_W, _H), Image.LANCZOS)

    buf = io.BytesIO()
    final.save(buf, format="PNG")
    buf.seek(0)
    return buf.read()
