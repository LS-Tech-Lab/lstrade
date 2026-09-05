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

    def resolve_tag_id(self, slug, timeout=8):
        """Resuelve el id numérico de un tag de Gamma por su slug (ej.
        'mlb', 'nba') -- mismo espíritu que WEATHER_TAG_ID de arriba, pero
        resuelto en vivo contra /tags/slug/{slug} en vez de hardcodeado.
        WEATHER_TAG_ID se pudo hardcodear porque alguien lo confirmó a
        mano contra una respuesta real de la API (ver comentario arriba);
        para MLB (y cualquier liga nueva) no hay esa confirmación todavía,
        así que se resuelve acá y el caller lo cachea (ver
        resolve_mlb_tag_id en mlb_signal_engine.py) para no pagar esta
        request en cada ciclo."""
        try:
            resp = self.session.get(f"{GAMMA_API}/tags/slug/{slug}", timeout=timeout)
            resp.raise_for_status()
            return resp.json().get("id")
        except Exception as e:
            log.warning(f"No se pudo resolver tag_id para slug={slug}: {e}")
            return None

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

    def fetch_order_book_liquidity(self, token_id, timeout=15):
        """Suma el notional (price*size) de bids+asks del order book de un token
        vía CLOB /book, como medida de liquidez real en $ para ESE token puntual.

        FIX (02/09/2026): polymarket_track_results.py usaba `liquidity` de
        fetch_market_by_condition_id() (Gamma) como gate de seguridad antes de
        resolver una señal. Como Gamma no soporta filtrar por condition_id (ver
        nota en fetch_market_by_condition_id más abajo), ese método siempre
        devolvía None -- y con la validación de conditionId que se agregó para
        arreglar el módulo de clima, pasó de "colar con datos de un mercado
        random" a "bloquear TODO siempre" -- ninguna de las señales de
        Polymarket llegaba ni a chequear si tocó target/stop. Este método evita
        depender de Gamma por completo: pega directo a la CLOB con el
        token_id real de la señal (que ya se usa más abajo para
        fetch_price_history), y de paso es más preciso que el agregado de
        Gamma porque refleja la profundidad real de ESE token, no un promedio
        del mercado. Devuelve None (no 0) si no se pudo obtener el book, para
        distinguir "sin datos" de "liquidez real es cero"."""
        try:
            resp = self.session.get(
                f"{CLOB_API}/book",
                params={"token_id": token_id},
                timeout=timeout
            )
            resp.raise_for_status()
            book = resp.json()
            total = 0.0
            for side in ("bids", "asks"):
                for level in book.get(side, []):
                    try:
                        total += float(level.get("price", 0)) * float(level.get("size", 0))
                    except (TypeError, ValueError):
                        continue
            return total
        except Exception as e:
            log.warning(f"Error fetching order book for token {token_id}: {e}")
            return None

    def fetch_order_book_snapshot(self, token_id, timeout=15):
        """Como fetch_order_book_liquidity, pero además del notional total
        devuelve el mejor ask y el mejor bid reales del book.

        AUDITORÍA (04/09/2026): weather_signal_engine.py elegía best_trade
        comparando la probabilidad del modelo contra `yes_price` de
        parse_market_for_analysis, que viene de outcomePrices de Gamma —
        el ÚLTIMO PRECIO OPERADO, no lo que cuesta comprar ahora. En un
        bucket barato e ilíquido (el perfil que ese motor busca) ese
        último trade puede tener horas y no reflejar el book actual. Este
        método pega al mismo endpoint que fetch_order_book_liquidity pero
        además extrae el mejor ask, para que el EV final se calcule contra
        un precio ejecutable de verdad. Devuelve None si no se pudo
        obtener el book (no un dict con ceros), para distinguir "sin
        datos" de "book real vacío"."""
        try:
            resp = self.session.get(
                f"{CLOB_API}/book",
                params={"token_id": token_id},
                timeout=timeout
            )
            resp.raise_for_status()
            book = resp.json()
            liquidity = 0.0
            asks, bids = [], []
            for level in book.get("asks", []):
                try:
                    price = float(level.get("price", 0))
                    size = float(level.get("size", 0))
                except (TypeError, ValueError):
                    continue
                liquidity += price * size
                if size > 0 and price > 0:
                    asks.append(price)
            for level in book.get("bids", []):
                try:
                    price = float(level.get("price", 0))
                    size = float(level.get("size", 0))
                except (TypeError, ValueError):
                    continue
                liquidity += price * size
                if size > 0 and price > 0:
                    bids.append(price)
            return {
                "liquidity": liquidity,
                "best_ask": min(asks) if asks else None,
                "best_bid": max(bids) if bids else None,
            }
        except Exception as e:
            log.warning(f"Error fetching order book snapshot for token {token_id}: {e}")
            return None

    def fetch_market_by_condition_id(self, condition_id, timeout=15):
        """Semana 3: Obtiene los datos actuales de un mercado específico para validar su liquidez.

        NOTA (02/09/2026): la Gamma API NO soporta filtrar /markets por condition
        id -- ni condition_id ni conditionId están documentados como filtro
        (docs.polymarket.com/api-reference/markets/get-markets solo lista
        id/slug directo, o filtros como tagId/closed/active). Un parámetro no
        reconocido se ignora en silencio (sin 400) y la API devuelve el primer
        mercado del listado por defecto -- por eso este método casi nunca
        traía el mercado real pedido. Se deja la validación de conditionId
        para que al menos falle de forma segura (devuelve None) en vez de
        devolver un mercado equivocado. Para resolver señales por condition_id
        de forma confiable, usar fetch_clob_market() más abajo, que sí soporta
        la búsqueda directa vía la CLOB API."""
        try:
            resp = self.session.get(
                f"{GAMMA_API}/markets",
                params={"conditionId": condition_id, "limit": 1},
                timeout=timeout
            )
            resp.raise_for_status()
            markets = resp.json()
            for m in markets:
                if m.get("conditionId") == condition_id:
                    return self.parse_market_for_analysis(m)
            if markets:
                log.warning(
                    f"fetch_market_by_condition_id: la API devolvió mercado(s) "
                    f"pero ninguno matchea conditionId={condition_id} -- filtro "
                    f"posiblemente ignorado de nuevo, revisar respuesta cruda."
                )
            return None
        except Exception as e:
            log.warning(f"Error fetching market by condition_id {condition_id}: {e}")
            return None

    def fetch_clob_market(self, condition_id, timeout=15):
        """Busca un mercado directo por condition_id vía la CLOB API.

        A diferencia de fetch_market_by_condition_id() (Gamma, ver nota arriba),
        acá el condition_id va en el PATH, no como query filter, y sí está
        documentado: GET https://clob.polymarket.com/markets/{condition_id}
        (docs.polymarket.com/developers/CLOB/markets/get-market). Devuelve el
        campo `closed` real y el precio del token YES -- lo mínimo que necesita
        run_weather_track_results() para saber si una señal ya resolvió de
        verdad. El esquema de la CLOB API es distinto al de Gamma (tokens es
        una lista de {token_id, outcome, price, winner}, no hay `liquidity` ni
        `outcomePrices` como en Gamma), así que este método devuelve un dict
        chico propio en vez de reusar parse_market_for_analysis()."""
        try:
            resp = self.session.get(f"{CLOB_API}/markets/{condition_id}", timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            if data.get("condition_id") != condition_id:
                log.warning(
                    f"fetch_clob_market: condition_id devuelto no matchea "
                    f"({data.get('condition_id')} != {condition_id})"
                )
                return None

            yes_price = 0.0
            for token in data.get("tokens", []):
                if str(token.get("outcome", "")).upper() == "YES":
                    yes_price = float(token.get("price", 0) or 0)
                    break

            return {
                "condition_id": data.get("condition_id"),
                "closed": bool(data.get("closed", False)),
                "active": bool(data.get("active", False)),
                "yes_price": yes_price,
            }
        except Exception as e:
            log.warning(f"Error fetching CLOB market {condition_id}: {e}")
            return None

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
                url = f"https://polymarket.com/event/{ev['slug']}" if ev.get("slug") else None
                weather_events.append({"title": title, "id": ev.get("id"), "url": url, "markets": markets})
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
                    url = f"https://polymarket.com/event/{ev['slug']}" if ev.get("slug") else None
                    weather_events.append({"title": title, "id": ev.get("id"), "url": url, "markets": markets})

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
                url = f"https://polymarket.com/event/{ev['slug']}" if ev.get("slug") else None
                weather_events.append({"title": title, "id": ev.get("id"), "url": url, "markets": markets})
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
            yes_label = "Sí"
            no_label = "No"

            # Buscar explícitamente YES y NO
            is_literal_yes_no = False
            for i, outcome in enumerate(outcomes):
                price = float(outcome_prices[i]) if i < len(outcome_prices) else 0.0
                token_id = token_ids[i] if i < len(token_ids) else None
                if outcome.upper() == "YES":
                    yes_price = price
                    yes_token_id = token_id
                    is_literal_yes_no = True
                elif outcome.upper() == "NO":
                    no_price = price
                    no_token_id = token_id
                    is_literal_yes_no = True

            # Fallback si el mercado no es Yes/No (ej: Over/Under o equipos) —
            # acá "YES"/"NO" son etiquetas internas nuestras, no lo que dice
            # la página: el outcome real es el nombre del equipo/jugador
            # (outcomes[0]/outcomes[1]), que es lo que hay que mostrarle al
            # usuario para que sepa qué opción tocar en Polymarket.
            if not is_literal_yes_no and len(outcome_prices) >= 2:
                yes_price = float(outcome_prices[0])
                no_price = float(outcome_prices[1])
                if len(outcomes) >= 2:
                    yes_label = outcomes[0]
                    no_label = outcomes[1]
                if not yes_token_id and len(token_ids) >= 2:
                    yes_token_id, no_token_id = token_ids[0], token_ids[1]

            return {
                "condition_id": market.get("conditionId"),
                # CLOB necesita el token id del outcome específico (77 dígitos), no el
                # condition_id — sin esto, /prices-history devuelve vacío siempre.
                "yes_token_id": yes_token_id,
                "no_token_id": no_token_id,
                "question": market.get("question", "Sin pregunta"),
                # Qué significa realmente comprar "YES" o "NO" en este mercado —
                # "Sí"/"No" en mercados binarios reales, o el nombre del
                # equipo/jugador en mercados tipo "A vs B" (ver arriba).
                "yes_label": yes_label,
                "no_label": no_label,
                "end_date": market.get("endDate"),
                "volume_24h": float(market.get("volume24hr", 0) or 0),
                "volume_total": float(market.get("volume", 0) or 0),
                "liquidity": float(market.get("liquidity", 0) or 0),
                "yes_price": yes_price,
                "no_price": no_price,
                "active": market.get("active", False),
                "closed": market.get("closed", False),
                "url": self._build_market_url(market),
            }
        except Exception as e:
            log.warning(f"Error parsing market: {e}")
            return None

    @staticmethod
    def _build_market_url(market):
        """
        Arma el link público a polymarket.com para este mercado.

        Polymarket sirve las páginas bajo /event/{event_slug}, no bajo el
        slug del mercado individual (que puede no resolver como URL propia,
        sobre todo en eventos multi-outcome). El objeto de /markets trae
        `events` (lista, casi siempre 1 elemento) con el slug del evento —
        se prioriza ese. Si no viene (o viene vacío), se cae al slug del
        propio mercado como mejor esfuerzo, que en eventos de un solo
        mercado (el caso común de los mercados YES/NO que sigue este bot)
        coincide con el slug del evento.
        """
        events = market.get("events") or []
        if events and events[0].get("slug"):
            return f"https://polymarket.com/event/{events[0]['slug']}"
        slug = market.get("slug")
        if slug:
            return f"https://polymarket.com/event/{slug}"
        return None
