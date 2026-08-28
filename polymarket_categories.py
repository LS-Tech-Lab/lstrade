"""
Categorización de mercados de Polymarket por keywords del texto de la
pregunta — heurística, no una taxonomía oficial de Gamma (que no expone una
categoría estructurada confiable en lo que ya trae parse_market_for_analysis).

Módulo compartido para que analyze_polymarket_categories.py (análisis
offline del backtest) y polymarket_main.py (filtro en producción) usen
exactamente las mismas reglas — si se definieran por separado en cada
archivo, con el tiempo terminan divisando y el filtro en vivo deja de
coincidir con lo que se validó en el backtest.
"""
import re

# (nombre, patrón) — se evalúa en orden, el primero que matchea gana.
# Las categorías más específicas van primero para evitar que una más
# genérica (ej. Macro) se coma preguntas que en realidad son de otra cosa
# (ej. clima).
CATEGORY_RULES = [
    ("Clima",                     re.compile(r"temperature|hottest|coldest|rain|snow|hurricane|heat wave|weather|degrees?\b|Fahrenheit|Celsius", re.I)),
    ("Lanzamientos / FDV",        re.compile(r"\bFDV\b|one day after launch|launch a token", re.I)),
    ("Valuaciones privadas",      re.compile(r"valuation hit|\(HIGH\)|\(LOW\)", re.I)),
    ("Objetivo de precio cripto", re.compile(r"\breach \$|\bdip to \$|market cap", re.I)),
    ("Política / elecciones",     re.compile(r"election|senat|vote|president|confirm|governor|congress|by-election", re.I)),
    ("Deportes / vanity",         re.compile(r"\bfight\b|attend|wedding|\bwin\b.*(match|game|fight)|UFC", re.I)),
    ("Macro / eventos",           re.compile(r"bank failure|hack over|open interest", re.I)),
    ("IA / tech",                 re.compile(r"Claude|OpenAI|Anthropic|GPT|Frontier Math|Opus|Gemini", re.I)),
]

FALLBACK_CATEGORY = "Otros / sin clasificar"


def categorize(question):
    if not question:
        return FALLBACK_CATEGORY
    for name, pattern in CATEGORY_RULES:
        if pattern.search(question):
            return name
    return FALLBACK_CATEGORY
