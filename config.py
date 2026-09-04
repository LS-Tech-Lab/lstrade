"""
Configuración central de Trader IA 24/7.
"""
import os
from dotenv import load_dotenv

load_dotenv()

def _bool(name, default=False):
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")

def _float(name, default):
    val = os.getenv(name)
    return float(val) if val not in (None, "") else default

def _int(name, default):
    val = os.getenv(name)
    return int(val) if val not in (None, "") else default

class Config:
    EXCHANGE_ID = os.getenv("EXCHANGE_ID", "binance")
    API_KEY = os.getenv("API_KEY", "")
    API_SECRET = os.getenv("API_SECRET", "")
    API_PASSWORD = os.getenv("API_PASSWORD", "")
    # NUEVO: ccxt sin `timeout` explícito espera hasta su default (10s) POR
    # CADA request HTTP individual — y fetch_ohlcv() dispara un load_markets()
    # implícito la primera vez que se usa en el proceso (ver exchange_client.py),
    # que en serverless es SIEMPRE la primera vez, porque cada invocación
    # arranca un proceso nuevo sin caché entre ciclos. Con 2+ símbolos, eso es
    # load_markets() + N×fetch_ohlcv(), cada uno pudiendo tardar hasta el
    # default — fácil de exceder el maxDuration de Vercel sin que ninguna
    # request individual "esté mal", solo que se van sumando. Bajar esto
    # fuerza que una request lenta falle rápido (se loguea en `errors` y se
    # sigue con el próximo símbolo) en vez de colgar todo el ciclo.
    EXCHANGE_TIMEOUT_MS = _int("EXCHANGE_TIMEOUT_MS", 8000)

    SYMBOLS = [s.strip() for s in os.getenv("SYMBOLS", "BTC/USDT,ETH/USDT").split(",") if s.strip()]
    TIMEFRAME = os.getenv("TIMEFRAME", "1h")
    CANDLE_LIMIT = _int("CANDLE_LIMIT", 100)

    RISK_PCT_PER_TRADE = _float("RISK_PCT_PER_TRADE", 1.0)
    MAX_EXPOSURE_PCT = _float("MAX_EXPOSURE_PCT", 20.0)
    MAX_DRAWDOWN_PCT = _float("MAX_DRAWDOWN_PCT", 8.0)
    MAX_DRAWDOWN_KILL_PCT = _float("MAX_DRAWDOWN_KILL_PCT", 15.0)
    MAX_VOLATILITY_PCT = _float("MAX_VOLATILITY_PCT", 4.0)
    ATR_STOP_MULT = _float("ATR_STOP_MULT", 1.5)
    MIN_RR = _float("MIN_RR", 1.8)

    # NUEVO: ATR_STOP_MULT adaptativo por régimen de volatilidad. Antes era un
    # número fijo sin importar si el mercado está calmo o violento — eso hace
    # que en regímenes de alta volatilidad el stop quede relativamente
    # "angosto" (más probabilidad de que lo saque el ruido) y en regímenes
    # calmos quede relativamente "ancho" (peor RR real del que parece). Con
    # ADAPTIVE_ATR_STOP=true, el múltiplo escala según qué tan alta esté la
    # volatilidad reciente (signal["volatility"], en %) respecto a
    # ATR_STOP_VOL_REF_PCT, entre ATR_STOP_MULT_MIN y ATR_STOP_MULT_MAX.
    ADAPTIVE_ATR_STOP = _bool("ADAPTIVE_ATR_STOP", False)
    ATR_STOP_VOL_REF_PCT = _float("ATR_STOP_VOL_REF_PCT", 1.0)
    ATR_STOP_MULT_MIN = _float("ATR_STOP_MULT_MIN", 1.0)
    ATR_STOP_MULT_MAX = _float("ATR_STOP_MULT_MAX", 2.5)
    
    # NUEVO: Filtro de Spread/Liquidez
    MAX_SPREAD_PCT = _float("MAX_SPREAD_PCT", 0.5)  # Máximo 0.5% de diferencia entre bid y ask

    # NUEVO: Límite de exposición correlacionada — máximo de posiciones abiertas
    # simultáneas en la MISMA dirección (LONG o SHORT). MAX_EXPOSURE_PCT ya
    # limita el % de equity comprometido, pero no distingue si esas posiciones
    # están correlacionadas (varias altcoins moviéndose junto con BTC son, en
    # la práctica, una sola apuesta direccional concentrada).
    MAX_CORRELATED_POSITIONS = _int("MAX_CORRELATED_POSITIONS", 5)

    LIVE_TRADING = _bool("LIVE_TRADING", False)
    AUTO_EXECUTE = _bool("AUTO_EXECUTE", False)
    ORDER_TYPE = os.getenv("ORDER_TYPE", "market")

    LOOP_INTERVAL_SECONDS = _int("LOOP_INTERVAL_SECONDS", 300)
    DB_PATH = os.getenv("DB_PATH", "trader_ia_247.db")

    # Horas mínimas antes de reavisar la misma señal de Polymarket si no cambió
    # de dirección ni subió de score de forma relevante (evita spam en Telegram)
    POLYMARKET_RESEND_COOLDOWN_HOURS = _float("POLYMARKET_RESEND_COOLDOWN_HOURS", 6.0)

    # Plan de salida sugerido para señales de Polymarket: mismo principio que
    # ATR_STOP_MULT/MIN_RR del módulo cripto, pero usando la volatilidad del
    # historial de precios de Polymarket en vez de ATR. Sin validar todavía
    # contra resultados reales — punto de partida, no un valor probado.
    POLYMARKET_STOP_VOL_MULT = _float("POLYMARKET_STOP_VOL_MULT", 3.0)
    POLYMARKET_TARGET_RR = _float("POLYMARKET_TARGET_RR", 1.5)

    # AUDITORÍA (03/09/2026, fix de polymarket_backtest.py): score mínimo
    # para que generate_polymarket_signal() devuelva señal. Antes vivía solo
    # como el default hardcodeado 0.06 dentro de la firma de la función
    # (nunca pasado explícito desde polymarket_main.py) mientras
    # polymarket_backtest.py pasaba un 0.03 propio hardcodeado -- dos
    # valores distintos sin que nada lo hiciera evidente, así que el
    # backtest terminaba midiendo una estrategia más permisiva que la que
    # corre en vivo. Se saca a config como única fuente para que ambos
    # coincidan siempre, igual que ya se hace con STOP_VOL_MULT/TARGET_RR
    # arriba y con POLYMARKET_MIN_CONFIDENCE más abajo.
    POLYMARKET_MIN_SCORE = _float("POLYMARKET_MIN_SCORE", 0.06)

    # AUDITORÍA (03/09/2026, pedido de reducir volumen y subir calidad):
    # $1,000 de liquidez era el piso hardcodeado en polymarket_main.py desde
    # el principio, sin revisar. Un book tan fino no solo tiene más riesgo
    # de slippage al ejecutar, también es más fácil que el precio se mueva
    # 2-5% con muy poco volumen real detrás -- exactamente el tipo de
    # "momentum" que generate_polymarket_signal ahora exige como factor
    # primario. Subir el piso de liquidez reduce candidatos ruidosos antes
    # de que lleguen siquiera al motor de señales.
    POLYMARKET_MIN_LIQUIDITY = _float("POLYMARKET_MIN_LIQUIDITY", 5000.0)

    # NUEVO: categorías de mercado (ver polymarket_categories.py) que no se
    # avisan más — basado en polymarket_backtest.py + analyze_polymarket_categories.py
    # sobre 216 trades reales: "Política / geopolítica" dio profit factor 0.80
    # con n=41 (pierde plata en promedio, muestra suficiente para no ser
    # ruido). El resto de categorías con muestra chica (Deportes/vanity,
    # Macro, IA/tech) todavía no tienen evidencia suficiente para excluirlas
    # — se dejan corriendo para seguir juntando datos.
    #
    # FIX: el string por defecto decía "Política / elecciones", que no
    # coincide con ningún nombre real de polymarket_categories.py (la
    # categoría se llama "Política / geopolítica") — el filtro nunca
    # excluyó nada en producción pese al comentario de arriba.
    #
    # 2026-09-01: agregada "Redes sociales / figuras públicas" — sobre 47
    # señales resueltas en polymarket_signals (Supabase, proyecto lstrade),
    # esa categoría dio 0% win rate y -5.00R en n=5 (todas perdedoras).
    # Muestra chica — sigue siendo la peor categoría del corte por lejos,
    # pero conviene revalidar cuando haya más señales antes de darla por
    # sentado como edge negativo estructural.
    #
    # 2026-09-02: agregada "Otros / sin clasificar" (el fallback de
    # polymarket_categories.py cuando ninguna regla matchea la pregunta).
    # Sobre 104 señales resueltas ese día, esta categoría fue apenas n=2
    # (ambas "stop") — muestra demasiado chica para hablar de edge negativo
    # real, a diferencia de "Redes sociales" arriba. Se excluye igual por
    # costo de oportunidad casi nulo: por definición un mercado que no
    # matcheó ninguna categoría conocida (deportes, esports, cripto, etc.)
    # no tiene un patrón identificable detrás del momentum de precio, así
    # que no hay razón a priori para esperar que la señal generada tenga
    # fundamento. Revisar de nuevo con más muestra si en algún momento se
    # quiere reactivar.
    #
    # 2026-09-02: agregada "Cripto — objetivo de precio". Esta categoría ya
    # está cubierta por el módulo cripto propio (ccxt sobre SYMBOLS, con su
    # propio motor de riesgo/ATR/stop) — dejar pasar además señales de
    # Polymarket sobre el mismo subyacente (ej. "price of Bitcoin above $X")
    # duplica exposición direccional a BTC/ETH sin agregar una fuente de
    # edge distinta. Se excluye por solapamiento de exposición, no porque
    # el corte de resultados ya la muestre negativa.
    POLYMARKET_EXCLUDED_CATEGORIES = [
        c.strip() for c in os.getenv(
            "POLYMARKET_EXCLUDED_CATEGORIES",
            "Política / geopolítica,Redes sociales / figuras públicas,Otros / sin clasificar,Cripto — objetivo de precio",
        ).split(",") if c.strip()
    ]

    # 2026-09-02: filtro de confianza mínima para avisar una señal de
    # Polymarket. confidence = round(score*20), 1-5 (ver
    # polymarket_signal_engine.py) — confidence=2 corresponde a un score
    # apenas por encima del piso (0.03) de generate_polymarket_signal().
    # Igual que POLYMARKET_STOP_VOL_MULT en su momento: punto de partida
    # sin validar todavía contra resultados reales, porque
    # polymarket_signals no guardaba score/confidence hasta ahora (ver
    # record_polymarket_signal) — no había forma de confirmar con datos
    # reales si las señales de confianza baja rendían peor. Se agregan esas
    # columnas en este mismo cambio para poder revisar esto con evidencia
    # dentro de unos días en vez de a ojo.
    #
    # AUDITORÍA (03/09/2026, pedido de reducir volumen y subir calidad): 3
    # era demasiado permisivo — confidence=3 corresponde a score≈0.15, que
    # con los umbrales viejos de generate_polymarket_signal se alcanzaba con
    # una ineficiencia de precio de apenas 2% (spread normal de book, no
    # necesariamente arbitraje real) o un momentum de sólo 3% en 12 velas
    # (ruido común). Se sube a 4 -- score≈0.175+ -- junto con el
    # endurecimiento de los umbrales primarios en polymarket_signal_engine.py
    # (ver ese archivo), para que pasen menos señales pero con ineficiencia o
    # momentum genuinamente más fuertes que el ruido típico del book.
    POLYMARKET_MIN_CONFIDENCE = _int("POLYMARKET_MIN_CONFIDENCE", 4)

    # NUEVO: módulo de análisis de clima (weather_signal_engine.py) — usa
    # fuentes oficiales gratis (NWS + METAR/TAF de aviationweather.gov) para
    # estimar la distribución de probabilidad de la máxima del día y
    # compararla contra los precios de los buckets de Polymarket. Corre
    # separado del ciclo de precio/momentum (/api/weather_cycle en app.py,
    # propio cron externo) porque las llamadas de red que necesita (NWS points +
    # forecast + METAR + TAF por evento) no entran cómodas en el
    # presupuesto de 10s del ciclo principal.
    WEATHER_ANALYSIS_ENABLED = _bool("WEATHER_ANALYSIS_ENABLED", True)

    # api.weather.gov exige un User-Agent identificable (no un navegador
    # genérico) — poné acá un contacto real (app + email/repo), si no NWS
    # puede empezar a bloquear las requests. Ver
    # https://www.weather.gov/documentation/services-web-api
    WEATHER_USER_AGENT = os.getenv(
        "WEATHER_USER_AGENT", "lstrade-weather-bot/1.0 (contacto no configurado)"
    )

    # Umbral de EV mínimo para avisar un bucket de clima. Más alto que el
    # umbral de precio/momentum a propósito: acá el "edge" depende de un
    # pronóstico meteorológico con error real (±1-2°F típico en el día),
    # no de un patrón de precio — conviene exigir más margen antes de
    # operar. Sin validar todavía contra resultados reales, igual que
    # POLYMARKET_STOP_VOL_MULT en su momento — punto de partida.
    WEATHER_MIN_EV = _float("WEATHER_MIN_EV", 0.15)

    # Piso de precio de mercado para considerar una señal operable (hallazgo
    # 01/09/2026: EV = mi_prob/precio - 1 se dispara a miles de % cuando el
    # precio está pegado al piso de Polymarket, ej. $0.0005 — no porque haya
    # ventaja real, sino porque dividir por casi-cero infla cualquier
    # probabilidad, por chica que sea. Un bucket tan "muerto" para el
    # mercado normalmente tampoco tiene book real detrás. Se descarta como
    # candidato a mejor señal (best_trade) por debajo de este precio, aunque
    # sigue apareciendo en el detalle de buckets del reporte para
    # referencia.
    #
    # AUDITORÍA (03/09/2026, tras 8 fallos de 10 en la primera corrida real):
    # 0.01 NO cumplía el propósito de arriba — es el piso real de precio de
    # Polymarket (ningún book cotiza más barato que $0.01), así que el
    # filtro nunca se activaba y el problema que el comentario describe
    # (EV inflado por dividir casi entre cero) seguía pasando en cada
    # corrida. Se sube a un piso que sí excluye contratos "muertos".
    WEATHER_MIN_PRICE = _float("WEATHER_MIN_PRICE", 0.05)

    # Desvío estándar base (°F) para repartir la masa de probabilidad entre
    # buckets adyacentes — punto de partida de la skill wu-airport-weather
    # ("normalmente ±1-2°F"). Ajustable sin tocar código a medida que se
    # calibre contra resultados reales.
    #
    # AUDITORÍA (03/09/2026): 1.6°F resultó demasiado angosto para un solo
    # día de datos reales — la skill de referencia advierte explícitamente
    # contra "overconfident single-bucket distributions" y exige que el
    # RANGO COMPLETO de confianza limpie el precio (regla de Silver), no
    # solo el punto central. Con sigma tan chico, casi toda la masa de
    # probabilidad caía en 1-2 buckets y generaba EV altos que no
    # sobrevivían al error real de pronóstico del día. Se sube a un valor
    # más conservador mientras se junta historial suficiente para calibrar
    # esto con datos en vez de a ojo.
    WEATHER_BASE_SIGMA_F = _float("WEATHER_BASE_SIGMA_F", 2.4)

    # Forzar una estación ICAO específica en vez de resolverla por el texto
    # del evento (STATION_MAP en weather_signal_engine.py) — útil para
    # debug o para cubrir una ciudad que el mapeo automático todavía no
    # tiene. Cuando se usa, el ajuste por trayectoria matutina asume UTC
    # (no se puede geocodificar sin lat/lon).
    WEATHER_STATION_OVERRIDE = os.getenv("WEATHER_STATION_OVERRIDE", "") or None

    # Horas mínimas antes de reavisar el mismo bucket de clima si el EV no
    # subió lo suficiente — mismo mecanismo que POLYMARKET_RESEND_COOLDOWN_HOURS
    # pero con ventana más corta porque el pronóstico se actualiza más rápido
    # que un patrón de precio.
    WEATHER_RESEND_COOLDOWN_HOURS = _float("WEATHER_RESEND_COOLDOWN_HOURS", 3.0)

    # AUDITORÍA (03/09/2026): a diferencia de polymarket_main.py
    # (MAX_SIGNALS_PER_CYCLE=1), run_weather_cycle() en app.py no tenía
    # ningún tope de cuántos best_trade se mandan por corrida -- podía
    # enviar y registrar hasta WEATHER_TOP_N (5) señales en un solo ciclo.
    # Con /api/weather_cycle disparado cada 30 min (ver README, sección de
    # cron-job.org) eso son 48 corridas/día × 5 = 240 avisos/día en el
    # techo teórico, muy por encima del volumen que llevó a bajar
    # MAX_SIGNALS_PER_CYCLE a 1 en el módulo de Polymarket por el mismo
    # motivo (ver ese comentario en polymarket_main.py). En la práctica el
    # número real es más bajo porque WEATHER_TOP_N ya filtra a las 5
    # ciudades con más liquidez y el cooldown de reenvío evita repetir la
    # MISMA ciudad seguido -- pero nada impedía que las 5 califberan y se
    # mandaran juntas en una sola corrida. Punto de partida sin validar
    # todavía contra resultados reales (no hay backtest de este módulo,
    # ver nota más abajo) -- se elige 2 en vez de 1 porque acá cada
    # "señal" es una ciudad distinta, no el mismo activo, así que hay más
    # razón a priori para no descartar automáticamente la segunda mejor
    # oportunidad del ciclo.
    MAX_WEATHER_SIGNALS_PER_CYCLE = _int("MAX_WEATHER_SIGNALS_PER_CYCLE", 2)

    # AUDITORÍA (04/09/2026, tras 20 señales cerradas con 15% de aciertos):
    # best_trade se elegía con yes_price de Gamma (outcomePrices), que es el
    # ÚLTIMO PRECIO OPERADO, no el ask real -- en un bucket barato e ilíquido
    # (el perfil que este motor busca) ese trade puede tener horas y estar
    # muy por debajo de lo que cuesta comprar ahora. Tampoco había piso de
    # liquidez: se calculaba `liquidity` por fila pero nunca se usaba para
    # descartar candidatos. Ahora, antes de fijar best_trade, se verifica el
    # candidato contra el order book real (fetch_order_book_snapshot) y se
    # exige este piso de liquidez en $ notional (bids+asks sumados).
    WEATHER_MIN_LIQUIDITY = _float("WEATHER_MIN_LIQUIDITY", 200.0)

    # Techo de sanidad para el EV verificado contra el book real. EV =
    # mi_prob/precio - 1 diverge fácil cuando el precio es muy bajo -- un EV
    # por encima de este techo es más probable un error de modelo (cola mal
    # calibrada) que una ventaja real explotable, mismo criterio que ya se
    # usó para los stops de Polymarket (ver auditoría de stops, 03/09/2026).
    # Punto de partida sin validar todavía contra resultados reales.
    WEATHER_MAX_EV = _float("WEATHER_MAX_EV", 3.0)

    NOTIFY_TELEGRAM = _bool("NOTIFY_TELEGRAM", False)
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
    APPROVAL_TIMEOUT_SECONDS = _int("APPROVAL_TIMEOUT_SECONDS", 900)
    # NUEVO: distinto del anterior — ese es para el modo VPS bloqueante
    # (ask_approval hace polling y espera esto antes de rechazar solo). Este
    # es para el modo serverless no bloqueante: si una decisión pendiente
    # (creada por /api/cycle, esperando que toques un botón en Telegram)
    # sigue sin resolver después de este tiempo, se vence sola en el
    # siguiente ciclo — sin esto, un aviso que se te pasa deja el bot entero
    # mudo para siempre (has_open_pending_decision() corta el ciclo antes de
    # llegar a generar señales nuevas O al heartbeat).
    PENDING_DECISION_EXPIRY_SECONDS = _int("PENDING_DECISION_EXPIRY_SECONDS", 3600)

    # Semana 3: Liquidez mínima requerida para marcar una señal de Polymarket como "resuelta" (target/stop).
    # Evita falsos positivos donde el precio "mid" toca el nivel pero no hay profundidad de libro para ejecutar.
    POLYMARKET_MIN_EXIT_LIQUIDITY = _float("POLYMARKET_MIN_EXIT_LIQUIDITY", 500.0)

    @classmethod
    def validate(cls):
        problems = []
        if cls.LIVE_TRADING and (not cls.API_KEY or not cls.API_SECRET):
            problems.append("LIVE_TRADING=true pero falta API_KEY/API_SECRET en .env")
        if not cls.SYMBOLS:
            problems.append("SYMBOLS está vacío")
        if cls.NOTIFY_TELEGRAM and (not cls.TELEGRAM_BOT_TOKEN or not cls.TELEGRAM_CHAT_ID):
            problems.append("NOTIFY_TELEGRAM=true pero falta TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID en .env")
        return problems
