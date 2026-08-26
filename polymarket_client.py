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

    def fetch_active_markets(self, limit=50, offset=0, closed=False):
        try:
            params = {
                "limit": limit,
                "offset": offset,
                "closed": str(closed).lower(),
                "active": "true",
                "order": "volume24hr",
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
            # La API devuelve outcomes y outcomePrices como strings JSON
            outcomes = json.loads(market.get("outcomes", "[]"))
            outcome_prices = json.loads(market.get("outcomePrices", "[]"))
            
            yes_price = 0.0
            no_price = 0.0
            yes_token_id = market.get("conditionId") # Fallback
            
            # Buscar explícitamente YES y NO
            for i, outcome in enumerate(outcomes):
                price = float(outcome_prices[i]) if i < len(outcome_prices) else 0.0
                if outcome.upper() == "YES":
                    yes_price = price
                elif outcome.upper() == "NO":
                    no_price = price
            
            # Fallback si el mercado no es Yes/No (ej: Over/Under o equipos)
            if yes_price == 0.0 and no_price == 0.0 and len(outcome_prices) >= 2:
                yes_price = float(outcome_prices[0])
                no_price = float(outcome_prices[1])

            return {
                "condition_id": market.get("conditionId"),
                "question": market.get("question", "Sin pregunta"),
                "end_date": market.get("endDate"),
                "volume_24h": float(market.get("volume24hr", 0) or 0),
                "liquidity": float(market.get("liquidity", 0) or 0),
                "yes_price": yes_price,
                "no_price": no_price,
                "active": market.get("active", False),
                "closed": market.get("closed", False),
            }
        except Exception as e:
            log.warning(f"Error parsing market: {e}")
            return None