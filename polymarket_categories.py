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
    # Esports va antes que Deportes: preguntas como "Counter-Strike: A vs B"
    # también matchean el patrón genérico de "vs", así que la categoría más
    # específica tiene que ganar primero.
    ("Esports",                   re.compile(r"Counter-Strike|CS:?GO|\bCS2\b|League of Legends|\bLoL\b|\bDota ?2?\b|Valorant|\bBO[135]\b|Cyber Games", re.I)),
    ("Deportes",                  re.compile(r"\bvs\.?\b|\bwin on \d{4}-\d{2}-\d{2}\b|O/U \d|\bwin\b.*\b(Open|Championship|Cup|Series|League|Bowl|Final|Wimbledon)\b|\bATP\b|\bUFC\b|\bfight\b", re.I)),
    ("Lanzamientos / FDV",        re.compile(r"\bFDV\b|one day after launch|launch a token", re.I)),
    ("Valuaciones privadas",      re.compile(r"valuation hit|\(HIGH\)|\(LOW\)", re.I)),
    # FIX: antes solo matcheaba "reach $"/"dip to $"/"market cap" — ninguna
    # pregunta real de Polymarket viene redactada así. El fraseo real es
    # "price of Bitcoin", "Bitcoin ... above $X" o "Bitcoin Up or Down".
    ("Cripto — objetivo de precio", re.compile(r"\bprice of (bitcoin|ethereum|btc|eth|solana|sol|xrp|doge)\b|\b(bitcoin|ethereum|btc|eth)\b.*\b(up or down|above \$|below \$)|reach \$|dip to \$|market cap", re.I)),
    ("Macro / tasas de interés",  re.compile(r"\bFed\b|interest rate|\bbps\b|inflation|\bGDP\b|jobs report|rate hike|rate cut", re.I)),
    ("Macro / eventos cripto",    re.compile(r"bank failure|hack over|open interest", re.I)),
    # FIX: geopolítica (ceasefire, invasión) caía en "Otros" — solo cubría
    # elecciones/votaciones, no conflictos internacionales.
    ("Política / geopolítica",    re.compile(r"election|senat|vote|president|confirm|governor|congress|by-election|invade|ceasefire|\bwar\b", re.I)),
    # NUEVO: preguntas sobre actividad de figuras públicas en redes
    # (ej. conteo de tweets) no tenían categoría propia.
    ("Redes sociales / figuras públicas", re.compile(r"\btweets?\b|\bpost(?:ed)? \d|Elon Musk|\bX posts?\b|Instagram|TikTok", re.I)),
    ("IA / tech",                 re.compile(r"Claude|OpenAI|Anthropic|GPT|Frontier Math|Opus|Gemini", re.I)),
    ("Entretenimiento / vanity",  re.compile(r"attend|wedding", re.I)),
]

FALLBACK_CATEGORY = "Otros / sin clasificar"


def categorize(question):
    if not question:
        return FALLBACK_CATEGORY
    for name, pattern in CATEGORY_RULES:
        if pattern.search(question):
            return name
    return FALLBACK_CATEGORY
