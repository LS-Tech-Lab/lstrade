"""
check_stop_noise.py — ¿Los stops están cortando señales que igual hubieran
ganado?

Recorre closed_trades (outcome='stop', r_multiple <= -0.9, o sea stops
"puros" — no breakeven/trailing) y, para cada uno, pide al exchange las
velas de las N horas siguientes a ts_closed para ver si el precio hubiera
llegado al target original si la posición hubiese seguido abierta.

CÓMO CORRERLO
-------------
1. Copiá este archivo a la raíz de tu repo lstrade (mismo nivel que
   config.py, exchange_client.py, supabase_db.py) — usa esos módulos tal
   cual los tenés, no reimplementa nada.
2. Corré con el mismo .env que ya usás para el bot (necesita SUPABASE_URL,
   SUPABASE_KEY, EXCHANGE_ID; NO necesita API_KEY/API_SECRET porque solo
   lee OHLCV público, pero si el exchange igual los pide para inicializar
   el cliente, ya los tenés en el .env):

     python check_stop_noise.py

   Opcional: horas hacia adelante a revisar (default 6) y mínimo de
   trades para no correr con muestra chica:

     python check_stop_noise.py --hours 12 --min-r -0.9

3. Requiere las mismas deps que el bot (ccxt, supabase, python-dotenv —
   ya están en requirements.txt).

QUÉ IMPRIME
-----------
Por cada trade "stop puro": si en las siguientes N horas el precio tocó el
target original (serial habría ganado igual) o no, y cuánto fue el MFE
(la mejor excursión a favor que llegó a tener después de cerrarse).
Al final, un resumen: cuántos de esos stops eran evitables.
"""
import argparse
import os
import sys
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

from config import Config          # noqa: E402  (mismo config que el bot)
from exchange_client import ExchangeClient  # noqa: E402
from supabase import create_client  # noqa: E402


def fetch_trades(supabase, min_r):
    resp = (
        supabase.table("closed_trades")
        .select("id,symbol,direction,entry_price,exit_price,r_multiple,ts_opened,ts_closed")
        .eq("outcome", "stop")
        .lte("r_multiple", min_r)
        .order("ts_closed", desc=False)
        .execute()
    )
    return resp.data or []


def target_price_from_trade(trade):
    """Reconstruye el target original a partir de entry/exit/r_multiple.

    exit_price == stop tocado, r_multiple ~ -1.0 en el stop puro, y
    MIN_RR es el múltiplo configurado (target = entry +/- stop_distance*MIN_RR).
    stop_distance = |entry - exit_price| / |r_multiple| (con r_multiple<0
    en un stop puro, abs da la distancia real).
    """
    entry = trade["entry_price"]
    exit_price = trade["exit_price"]
    r = trade["r_multiple"]
    if r == 0:
        return None
    stop_distance = abs(entry - exit_price) / abs(r)
    sign = 1 if trade["direction"] == "LONG" else -1
    return entry + sign * stop_distance * Config.MIN_RR, stop_distance


def analyze(exchange, trade, hours_forward):
    symbol = trade["symbol"]
    direction = trade["direction"]
    target, stop_distance = target_price_from_trade(trade)
    if target is None:
        return None

    ts_closed = datetime.fromisoformat(trade["ts_closed"].replace("Z", "+00:00"))
    since_ms = int(ts_closed.timestamp() * 1000)
    limit = max(4, int(hours_forward / _timeframe_hours()) + 2)

    try:
        candles = exchange.fetch_ohlcv(symbol, timeframe=Config.TIMEFRAME, limit=limit, since=since_ms)
    except Exception as e:
        print(f"  [WARN] no se pudo bajar OHLCV de {symbol}: {e}")
        return None

    if not candles:
        return None

    # AUDITORÍA (17/09/2026, pedido del usuario -- ventana de 24h reveló
    # varios SHORT con MFE grande pero hit_target_after=False, matemáticamente
    # inconsistente si las velas realmente arrancan en `since` y van
    # ordenadas -- posible señal de que el exchange no honró `since` como se
    # esperaba (quirk conocido de ccxt con algunos exchanges) o de que
    # llegaron desordenadas. Dos chequeos baratos para no confiar ciego en
    # el resultado:
    candles = sorted(candles, key=lambda c: c["ts"])
    first_gap_hours = abs(candles[0]["ts"] - since_ms) / 3_600_000
    if first_gap_hours > 2 * _timeframe_hours():
        print(
            f"  [SOSPECHOSO] {symbol} {trade['ts_closed']}: la primera vela devuelta "
            f"está a {first_gap_hours:.1f}h de `since` (se esperaba ~0) -- el exchange "
            f"puede no haber respetado el punto de partida pedido, no confiar en este resultado."
        )

    hit_target = False
    best_favorable = trade["exit_price"]
    for c in candles:
        if direction == "LONG":
            best_favorable = max(best_favorable, c["h"])
            if c["h"] >= target:
                hit_target = True
                break
        else:
            best_favorable = min(best_favorable, c["l"])
            if c["l"] <= target:
                hit_target = True
                break

    mfe_r = abs(best_favorable - trade["exit_price"]) / stop_distance if stop_distance else 0.0
    if direction == "SHORT":
        # si el precio subió tras un SHORT cerrado, eso no es "a favor"
        if best_favorable > trade["exit_price"]:
            mfe_r = 0.0
    else:
        if best_favorable < trade["exit_price"]:
            mfe_r = 0.0

    # Distancia (en unidades de stop_distance) desde exit_price hasta el
    # target reconstruido. Si mfe_r la superó, el bucle de arriba TENÍA que
    # haber marcado hit_target=True en esa misma vela -- si no lo hizo, algo
    # no cierra (ver el chequeo de `since` más arriba; puede ser la misma
    # causa, o una vela con high/low invertido, o `Config.MIN_RR` real
    # distinto al usado para reconstruir el target de esta señal en
    # particular). Se marca en vez de fallar en silencio.
    target_dist_from_exit = abs(target - trade["exit_price"]) / stop_distance if stop_distance else 0.0
    sospechoso = (not hit_target) and mfe_r >= target_dist_from_exit
    if sospechoso:
        print(
            f"  [SOSPECHOSO] {symbol} {trade['ts_closed']}: mfe_r={mfe_r:.2f} >= "
            f"distancia al target ({target_dist_from_exit:.2f}) pero hit_target_after=False "
            f"-- no debería ser matemáticamente posible, no confiar en esta fila sin revisar."
        )

    return {
        "symbol": symbol,
        "direction": direction,
        "ts_closed": trade["ts_closed"],
        "exit_price": trade["exit_price"],
        "target": target,
        "hit_target_after": hit_target,
        "mfe_r_after_stop": round(mfe_r, 2),
        "sospechoso": sospechoso,
    }


def _timeframe_hours():
    tf = Config.TIMEFRAME
    unit = tf[-1]
    n = int(tf[:-1])
    return {"m": n / 60, "h": n, "d": n * 24}.get(unit, 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=6.0, help="Horas hacia adelante a revisar tras el stop")
    ap.add_argument("--min-r", type=float, default=-0.9, help="Solo trades con r_multiple <= este valor (stops puros)")
    ap.add_argument("--output", type=str, default=None, help="Ruta de CSV opcional con el detalle (para subir como artifact en CI)")
    args = ap.parse_args()

    supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
    exchange = ExchangeClient(Config)

    trades = fetch_trades(supabase, args.min_r)
    if not trades:
        print("No hay trades 'stop puro' con ese filtro. Nada para analizar.")
        return

    print(f"Analizando {len(trades)} stops puros (r_multiple <= {args.min_r}), mirando {args.hours}h después de cada cierre...\n")

    results = []
    for t in trades:
        r = analyze(exchange, t, args.hours)
        if r:
            results.append(r)
            flag = "⚠️ HUBIERA GANADO" if r["hit_target_after"] else "ok (no recuperó)"
            print(f"[{r['ts_closed']}] {r['symbol']} {r['direction']}: {flag} "
                  f"(MFE post-stop: {r['mfe_r_after_stop']}R)")
        time.sleep(exchange.exchange.rateLimit / 1000 if hasattr(exchange.exchange, "rateLimit") else 0.2)

    if not results:
        print("No se pudo bajar OHLCV para ninguno (revisar conexión/símbolos).")
        return

    if args.output:
        import csv
        with open(args.output, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(results[0].keys()))
            writer.writeheader()
            writer.writerows(results)
        print(f"Detalle guardado en {args.output}\n")

    n_recovered = sum(1 for r in results if r["hit_target_after"])
    pct = 100 * n_recovered / len(results)
    n_sospechosos = sum(1 for r in results if r.get("sospechoso"))
    print(f"\n=== RESUMEN ===")
    print(f"Analizados: {len(results)}")
    print(f"Hubieran llegado al target igual (posible corte por ruido): {n_recovered} ({pct:.1f}%)")
    if n_sospechosos:
        print(f"⚠️  {n_sospechosos} fila(s) marcadas [SOSPECHOSO] arriba -- inconsistencia matemática "
              f"entre MFE y hit_target_after, revisar antes de confiar en el {pct:.1f}% de arriba.")
    avg_mfe = sum(r["mfe_r_after_stop"] for r in results) / len(results)
    print(f"MFE promedio post-stop (entre los que NO llegaron al target): "
          f"{sum(r['mfe_r_after_stop'] for r in results if not r['hit_target_after']) / max(1, len(results) - n_recovered):.2f}R")
    print(f"MFE promedio general: {avg_mfe:.2f}R")


if __name__ == "__main__":
    sys.exit(main())
