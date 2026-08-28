"""
Cliente de Polymarket — API pública Gamma (solo lectura, sin autenticación).
Polymarket usa la red Polygon. Los precios son probabilidades (0.00 a 1.00).
"""
import requests
import logging
import json
import time

log = logging.getLogger("polymarket_client")

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

class PolymarketClient:
    def __init__(self, config):
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "lstrade-polymarket/1.0",
            "Accept": "application/json"
        })

    def fetch_active_markets(self, limit=50, offset=0, closed=False, active=None):
        """
        `active` se ajusta automáticamente según `closed` si no se pasa
        explícito: un mercado no puede estar "activo" (abierto a trading) Y
        "cerrado" (resuelto) a la vez en el modelo de datos de Polymarket —
        pedir closed=true con active=true (como hacía esto antes, siempre
        fijo) es una combinación contradictoria que devuelve resultados
        inconsistentes o vacíos.
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
            resp = self.session.get(f"{GAMMA_API}/markets", params=params, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            log.warning(f"Error fetching markets: {e}")
            return []

    def fetch_price_history(self, token_id, interval="1h", fidelity=60):
        try:
            resp = self.session.get(
                f"{CLOB_API}/prices-history",
                params={"market": token_id, "interval": interval, "fidelity": fidelity},
                timeout=15
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("history", [])
        except Exception as e:
            log.warning(f"Error fetching price history: {e}")
            return []

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