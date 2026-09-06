"""
Categorización de mercados de Polymarket por keywords del texto de la
pregunta — heurística, no una taxonomía oficial de Gamma (que no expone una
categoría estructurada confiable en lo que ya trae parse_market_for_analysis).

Módulo compartido para que analyze_polymarket_categories.py (análisis
offline del backtest) y polymarket_main.py (filtro en producción) usen
exactamente las mismas reglas — si se definieran por separado en cada
archivo, con el tiempo terminan divisando y el filtro en vivo deja de
coincidir con lo que se validó en el backtest.

Las reglas en sí (nombre + patrón) viven en dashboard/polymarket_categories.json,
NO acá — ese mismo JSON lo lee también dashboard/app/api/data/route.js
(JS) para las stats por categoría. Antes había dos copias hardcodeadas
(una en Python, otra en JS) que había que tocar a la par a mano; con el
JSON como única fuente, cambiar una regla es un solo edit y ambos lados
quedan sincronizados automáticamente.
"""
import json
import re
from pathlib import Path

_RULES_PATH = Path(__file__).resolve().parent / "dashboard" / "polymarket_categories.json"


def _load_rules():
    with open(_RULES_PATH, encoding="utf-8") as f:
        spec = json.load(f)
    # Se evalúa en orden, el primero que matchea gana. Las categorías más
    # específicas van primero en el JSON para evitar que una más genérica
    # (ej. Macro) se coma preguntas que en realidad son de otra cosa
    # (ej. clima). Ver comentarios de cada regla en el JSON.
    rules = [(r["name"], re.compile(r["pattern"], re.I)) for r in spec["rules"]]
    return rules, spec["fallback"]


CATEGORY_RULES, FALLBACK_CATEGORY = _load_rules()


def categorize(question):
    if not question:
        return FALLBACK_CATEGORY
    for name, pattern in CATEGORY_RULES:
        if pattern.search(question):
            return name
    return FALLBACK_CATEGORY
