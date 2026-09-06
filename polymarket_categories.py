{
  "_comment": "Fuente única de reglas de categorización de mercados de Polymarket. La lee polymarket_categories.py (Python, motor de señales) y dashboard/app/api/data/route.js (JS, dashboard). Se evalúa en orden: la primera regla que matchea gana.",
  "fallback": "Otros / sin clasificar",
  "rules": [
    {
      "name": "Clima",
      "pattern": "temperature|hottest|coldest|\\brain\\b|\\bsnow\\b|hurricane|heat wave|weather|degrees?\\b|Fahrenheit|Celsius",
      "note": "FIX (06/09/2026): 'rain' y 'snow' sin límite de palabra matcheaban como substring dentro de cualquier palabra que los contuviera -- confirmado en producción: 'Russia x Ukraine ceasefire agreement...' caía acá por 'Uk-RAIN-e', ganándole a la regla de 'Política / geopolítica' (que sí tiene 'ceasefire') solo por estar antes en el orden de evaluación. Como Clima y Política están las dos en POLYMARKET_EXCLUDED_CATEGORIES esto no cambió qué se operó en este caso puntual, pero cualquier mercado con 'train'/'grain'/'brain'/'terrain'/'sprain' en la pregunta se habría categorizado como Clima y quedado excluido de operar sin ninguna razón real."
    },
    {
      "name": "Esports",
      "pattern": "Counter-Strike|CS:?GO|\\bCS2\\b|League of Legends|\\bLoL\\b|\\bDota ?2?\\b|Valorant|\\bBO[135]\\b|Cyber Games",
      "note": "Va antes que Deportes: preguntas como \"Counter-Strike: A vs B\" también matchean el patrón genérico de \"vs\", así que la categoría más específica tiene que ganar primero."
    },
    {
      "name": "Deportes",
      "pattern": "\\bvs\\.?\\b|\\bwin on \\d{4}-\\d{2}-\\d{2}\\b|O/U \\d|\\bwin\\b.*\\b(Open|Championship|Cup|Series|League|Bowl|Final|Wimbledon)\\b|\\bATP\\b|\\bUFC\\b|\\bfight\\b"
    },
    {
      "name": "Lanzamientos / FDV",
      "pattern": "\\bFDV\\b|one day after launch|launch a token"
    },
    {
      "name": "Valuaciones privadas",
      "pattern": "valuation hit|\\(HIGH\\)|\\(LOW\\)"
    },
    {
      "name": "Cripto — objetivo de precio",
      "pattern": "\\bprice of (bitcoin|ethereum|btc|eth|solana|sol|xrp|doge)\\b|\\b(bitcoin|ethereum|btc|eth)\\b.*\\b(up or down|above \\$|below \\$)|reach \\$|dip to \\$|market cap",
      "note": "FIX: antes solo matcheaba \"reach $\"/\"dip to $\"/\"market cap\" — ninguna pregunta real de Polymarket viene redactada así. El fraseo real es \"price of Bitcoin\", \"Bitcoin ... above $X\" o \"Bitcoin Up or Down\"."
    },
    {
      "name": "Macro / tasas de interés",
      "pattern": "\\bFed\\b|interest rate|\\bbps\\b|inflation|\\bGDP\\b|jobs report|rate hike|rate cut"
    },
    {
      "name": "Macro / eventos cripto",
      "pattern": "bank failure|hack over|open interest"
    },
    {
      "name": "Política / geopolítica",
      "pattern": "election|senat|vote|president|confirm|governor|congress|by-election|invade|ceasefire|\\bwar\\b",
      "note": "FIX: geopolítica (ceasefire, invasión) caía en \"Otros\" — antes solo cubría elecciones/votaciones, no conflictos internacionales."
    },
    {
      "name": "Redes sociales / figuras públicas",
      "pattern": "\\btweets?\\b|\\bpost(?:ed)? \\d|Elon Musk|\\bX posts?\\b|Instagram|TikTok",
      "note": "NUEVO: preguntas sobre actividad de figuras públicas en redes (ej. conteo de tweets) no tenían categoría propia."
    },
    {
      "name": "IA / tech",
      "pattern": "Claude|OpenAI|Anthropic|GPT|Frontier Math|Opus|Gemini"
    },
    {
      "name": "Entretenimiento / vanity",
      "pattern": "attend|wedding"
    }
  ]
}
