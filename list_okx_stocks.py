"""
Lista los pares de acciones tokenizadas (xStocks) que OKX expone en spot vía
ccxt, con el nombre EXACTO que hay que poner en STOCK_SYMBOLS.

Uso:  python list_okx_stocks.py            (todos los x*/USDT)
      python list_okx_stocks.py MU CRCL    (solo los que contengan esos textos)
"""
import sys
import ccxt

ex = ccxt.okx({"options": {"fetchCurrencies": False}})
markets = ex.load_markets()
filtros = [a.upper() for a in sys.argv[1:]]

filas = []
for sym, m in markets.items():
    if not m.get("spot") or not m.get("active", True) or m.get("quote") != "USDT":
        continue
    base = m.get("base", "")
    if not (base.startswith("x") or base.startswith("X")):
        continue
    if filtros and not any(f in base.upper() for f in filtros):
        continue
    mn = (m.get("limits", {}).get("cost", {}) or {}).get("min")
    filas.append((sym, mn))

for sym, mn in sorted(filas):
    print(f"{sym:<18} mínimo por orden: {mn}")
print(f"\n{len(filas)} pares. Copiá los nombres tal cual a STOCK_SYMBOLS (separados por coma).")
