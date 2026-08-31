"""
Cliente de Polymarket — API pública Gamma (solo lectura, sin autenticación).
Polymarket usa la red Polygon. Los precios son probabilidades (0.00 a 1.00).
"""
import requests
import logging
import json
import re
import time

log = logging.getLogger("polymarket_client")

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# Confirmado contra /tags/slug/weather en la API real: {"id":"84","label":"Weather"}.
# Es el filtro principal en fetch_weather_events — mucho más preciso y barato
# (1 sola llamada) que paginar y adivinar por palabra clave. El regex de abajo
# se conserva como respaldo: si algún evento nuevo todavía no tiene el tag
# aplicado, o si Polymarket cambia el ID en el futuro, no nos quedamos sin
# datos — sigue funcionando, solo que gastando más requests.
WEATHER_TAG_ID = "84"

_WEATHER_EVENT_PATTERN = re.compile(
    r"temperature|hottest|coldest|rain|snow|hurricane|heat wave|weather|degrees?\b|Fahrenheit|Celsius",
    re.I,
)

class PolymarketClient:
    def __init__(self, config):
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "lstrade-polymarket/1.0",
            "Accept": "application/json"
        })

    def fetch_active_markets(self, limit=50, offset=0, closed=False, active=None, extra_params=None, timeout=15):
        """
        `active` se ajusta automáticamente según `closed` si no se pasa
        explícito: un mercado no puede estar "activo" (abierto a trading) Y
        "cerrado" (resuelto) a la vez en el modelo de datos de Polymarket —
        pedir closed=true con active=true (como hacía esto antes, siempre
        fijo) es una combinación contradictoria que devuelve resultados
        inconsistentes o vacíos.

        `extra_params`: dict opcional para filtros adicionales de Gamma
        (ej. end_date_min/end_date_max) — se pasan tal cual, sin validar,
        porque no están documentados de forma confiable; ver
        polymarket_backtest.py para el uso defensivo que valida si Gamma
        realmente los está respetando.
        """
        if active is None:
            active = "false" if closed else "true"
        # Para mercados cerrados, volume24hr suele ser 0 (ya no hay trading) —
        # ordenar por volumen total tiene más sentido para priorizar los
        # mercados con más historial real.
        order_field = "volume" if closed else "volume24hr"
        try:
            params = {
                "limit": limit,
                "offset": offset,
                "closed": str(closed).lower(),
                "active": active if isinstance(active, str) else str(active).lower(),
                "order": order_field,
                "ascending": "false"
            }
            if extra_params:
                params.update(extra_params)
            resp = self.session.get(f"{GAMMA_API}/markets", params=params, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            log.warning(f"Error fetching markets: {e}")
            return []

    def fetch_price_history(self, token_id, interval="1h", fidelity=60, timeout=15):
        try:
            resp = self.session.get(
                f"{CLOB_API}/prices-history",
                params={"market": token_id, "interval": interval, "fidelity": fidelity},
                timeout=timeout
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("history", [])
        except Exception as e:
            log.warning(f"Error fetching price history: {e}")
            return []

    def fetch_weather_events(self, limit=20, timeout=15, max_pages=6, page_size=100,
                              time_budget_seconds=None):
        """
        Trae eventos de clima AGRUPADOS con sus mercados (buckets de
        temperatura) — a diferencia de fetch_active_markets, que devuelve
        mercados sueltos sin agrupar por evento. weather_signal_engine
        necesita todos los buckets de un mismo día/ciudad juntos para
        construir la distribución de probabilidad (STEP 2 de la skill
        wu-airport-weather), así que esto pega contra /events en vez de
        /markets.

        Método principal: filtrar por WEATHER_TAG_ID (confirmado contra la
        API real: id=84, label="Weather") — una sola llamada, sin ambigüedad,
        y no confunde mercados deportivos que mencionan "weather" en el
        título (ej. "TOUR Championship: Weather Delay?") con clima real,
        porque usa la categoría que Polymarket ya les asignó.

        Si esa llamada falla o devuelve vacío (¿tag distinto en el futuro?
        ¿evento nuevo sin tag todavía?), cae a _fetch_weather_events_by_keyword
        como respaldo — más lento (pagina y filtra por palabra clave) pero no
        depende de que el tag_id siga siendo válido para siempre.
        """
        by_tag = self._fetch_weather_events_by_tag(limit=limit, timeout=timeout)
        if by_tag:
            return by_tag

        log.info("fetch_weather_events: tag_id=%s no devolvió nada, usando fallback por palabra clave", WEATHER_TAG_ID)
        return self._fetch_weather_events_by_keyword(
            limit=limit, timeout=timeout, max_pages=max_pages,
            page_size=page_size, time_budget_seconds=time_budget_seconds,
        )

    def _fetch_weather_events_by_tag(self, limit=20, timeout=15):
        try:
            resp = self.session.get(
                f"{GAMMA_API}/events",
                params={
                    "tag_id": WEATHER_TAG_ID,
                    "limit": limit,
                    "active": "true",
                    "closed": "false",
                    "order": "volume24hr",
                    "ascending": "false",
                },
                timeout=timeout,
            )
            resp.raise_for_status()
            events = resp.json()
        except Exception as e:
            log.warning(f"Error fetching events by tag_id={WEATHER_TAG_ID}: {e}")
            return []

        weather_events = []
        for ev in events:
            markets = []
            for mk in ev.get("markets", []) or []:
                parsed = self.parse_market_for_analysis(mk)
                if parsed:
                    markets.append(parsed)
            if markets:
                title = ev.get("title") or ev.get("ticker") or ""
                weather_events.append({"title": title, "id": ev.get("id"), "markets": markets})
        return weather_events

    def _fetch_weather_events_by_keyword(self, limit=20, timeout=15, max_pages=6, page_size=100,
                                          time_budget_seconds=None):
        """
        Respaldo de fetch_weather_events. Antes esto pedía solo los 20
        eventos con MÁS volumen de TODO Polymarket (política, cripto,
        deportes...) y de ahí filtraba por palabra clave — el clima casi
        nunca gana ese ranking global, así que la función devolvía "sin
        eventos" casi siempre aunque hubiera cientos de mercados de clima
        activos. Ahora pagina por `created_at` descendente (más estable que
        volumen para no perderse eventos recién creados con poco volumen
        todavía) hasta juntar suficientes candidatos de clima o agotar
        max_pages/time_budget_seconds.
        """
        started = time.monotonic()
        weather_events = []
        offset = 0
        for _ in range(max_pages):
            if time_budget_seconds is not None and (time.monotonic() - started) > time_budget_seconds:
                break
            try:
                resp = self.session.get(
                    f"{GAMMA_API}/events",
                    params={
                        "limit": page_size,
                        "offset": offset,
                        "active": "true",
                        "closed": "false",
                        "order": "createdAt",
                        "ascending": "false",
                    },
                    timeout=timeout,
                )
                resp.raise_for_status()
                page = resp.json()
            except Exception as e:
                log.warning(f"Error fetching events (offset={offset}): {e}")
                break

            if not page:
                break

            for ev in page:
                title = ev.get("title") or ev.get("ticker") or ""
                if not _WEATHER_EVENT_PATTERN.search(title):
                    continue
                markets = []
                for mk in ev.get("markets", []) or []:
                    parsed = self.parse_market_for_analysis(mk)
                    if parsed:
                        markets.append(parsed)
                if markets:
                    weather_events.append({"title": title, "id": ev.get("id"), "markets": markets})

            if len(weather_events) >= limit or len(page) < page_size:
                break
            offset += page_size

        return weather_events[:limit]

    def _fetch_weather_events_by_volume_LEGACY(self, limit=20, timeout=15):
        """Implementación anterior — se deja documentada, no se usa. Solo
        miraba el top N por volumen24h en vez de paginar; ver docstring de
        fetch_weather_events de arriba para el porqué del cambio."""
        try:
            resp = self.session.get(
                f"{GAMMA_API}/events",
                params={
                    "limit": limit,
                    "active": "true",
                    "closed": "false",
                    "order": "volume24hr",
                    "ascending": "false",
                },
                timeout=timeout,
            )
            resp.raise_for_status()
            events = resp.json()
        except Exception as e:
            log.warning(f"Error fetching events: {e}")
            return []

        weather_events = []
        for ev in events:
            title = ev.get("title") or ev.get("ticker") or ""
            if not _WEATHER_EVENT_PATTERN.search(title):
                continue
            markets = []
            for mk in ev.get("markets", []) or []:
                parsed = self.parse_market_for_analysis(mk)
                if parsed:
                    markets.append(parsed)
            if markets:
                weather_events.append({"title": title, "id": ev.get("id"), "markets": markets})
        return weather_events

    def parse_market_for_analysis(self, market):
        try:
            # La API devuelve outcomes, outcomePrices y clobTokenIds como strings JSON,
            # los tres en el mismo orden (mismo índice = mismo outcome)
            outcomes = json.loads(market.get("outcomes", "[]"))
            outcome_prices = json.loads(market.get("outcomePrices", "[]"))
            try:
                token_ids = json.loads(market.get("clobTokenIds", "[]"))
            except (json.JSONDecodeError, TypeError):
                token_ids = []

            yes_price = 0.0
            no_price = 0.0
            yes_token_id = None
            no_token_id = None

            # Buscar explícitamente YES y NO
            for i, outcome in enumerate(outcomes):
                price = float(outcome_prices[i]) if i < len(outcome_prices) else 0.0
                token_id = token_ids[i] if i < len(token_ids) else None
                if outcome.upper() == "YES":
                    yes_price = price
                    yes_token_id = token_id
                elif outcome.upper() == "NO":
                    no_price = price
                    no_token_id = token_id

            # Fallback si el mercado no es Yes/No (ej: Over/Under o equipos)
            if yes_price == 0.0 and no_price == 0.0 and len(outcome_prices) >= 2:
                yes_price = float(outcome_prices[0])
                no_price = float(outcome_prices[1])
                if not yes_token_id and len(token_ids) >= 2:
                    yes_token_id, no_token_id = token_ids[0], token_ids[1]

            return {
                "condition_id": market.get("conditionId"),
                # CLOB necesita el token id del outcome específico (77 dígitos), no el
                # condition_id — sin esto, /prices-history devuelve vacío siempre.
                "yes_token_id": yes_token_id,
                "no_token_id": no_token_id,
                "question": market.get("question", "Sin pregunta"),
                "end_date": market.get("endDate"),
                "volume_24h": float(market.get("volume24hr", 0) or 0),
                "volume_total": float(market.get("volume", 0) or 0),
                "liquidity": float(market.get("liquidity", 0) or 0),
                "yes_price": yes_price,
                "no_price": no_price,
                "active": market.get("active", False),
                "closed": market.get("closed", False),
            }
        except Exception as e:
            log.warning(f"Error parsing market: {e}")
            return None
