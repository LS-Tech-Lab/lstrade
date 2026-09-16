"""
Verifica si el stop-loss fijo del 20% (WEATHER_MLB_STOP_LOSS_PCT, config.py)
está cortando señales de MLB que en realidad habrían resuelto ganadoras.

MOTIVACIÓN (16/09/2026, pedido del usuario -- "veo que se activan mucho y
quiero confirmar que no corten señales ganadoras por simples variaciones
porcentuales"): a diferencia de cripto (ATR_STOP_MULT/ADAPTIVE_ATR_STOP en
risk_manager.py) y Polymarket genérico (stop_distance = entry_price *
volatility * stop_vol_mult, capado por MAX_STOP_LOSS_PCT, en
polymarket_signal_engine.py), el stop de MLB/clima es un porcentaje FIJO del
precio de entrada (20%) sin importar la volatilidad propia del mercado --
exactamente el patrón que ya se corrigió en los otros dos módulos.

Diagnóstico rápido sobre outcome (Supabase, mlb_signals, 16/09/2026):
  win=44  loss=72  stop=90   -> el stop-loss es la causa de cierre MÁS
  frecuente (43% de las señales resueltas), más que loss y casi el doble
  que win.

Este script resuelve, para cada señal cerrada por stop, qué pasó REALMENTE
en el partido (vía fetch_game_result() de mlb_signal_engine.py, ya usado en
producción para resolver señales abiertas) y compara contra el lado que se
había comprado (direction/home_team/away_team). Así se puede saber qué
fracción de los stops eran en realidad señales ganadoras cortadas antes de
tiempo, en vez de asumirlo.

Uso:
    python analyze_mlb_stop_losses.py                  # todo el historial de stops
    python analyze_mlb_stop_losses.py --days 14         # últimos 14 días
    python analyze_mlb_stop_losses.py --csv out.csv     # exporta detalle por señal
"""
import argparse
import csv as csv_module
import os
import sys
import time

from supabase import create_client

from mlb_signal_engine import fetch_game_result

DEFAULT_TIMEOUT = 6


def fetch_stopped_signals(client, since_ts=None):
    q = (
        client.table("mlb_signals")
        .select("id,game_pk,home_team,away_team,direction,my_prob,market_price,stop,exit_price,ts_signaled,ts_resolved")
        .eq("outcome", "stop")
        .order("ts_signaled", desc=True)
    )
    if since_ts:
        q = q.gte("ts_signaled", since_ts)
    return q.execute().data or []


def would_have_won(direction, result):
    """direction == 'YES' compra el HOME (ver mlb_signal_engine.py línea
    ~1011: side_team = home_team si direction=='YES' si no away_team)."""
    return result["home_won"] if direction == "YES" else result["away_won"]


def r_multiple_at_resolution(entry, stop, won):
    """Mismo criterio que ya usa polymarket_track_results.py: r_multiple =
    (precio_final - entrada) / distancia_al_stop, con precio_final=1 si
    ganó, 0 si perdió (el mercado resuelve al extremo, no queda a medio
    camino)."""
    stop_distance = abs(entry - stop)
    if stop_distance <= 0:
        return None
    final_price = 1.0 if won else 0.0
    return (final_price - entry) / stop_distance


def r_multiple_at_stop(entry, exit_price, stop):
    stop_distance = abs(entry - stop)
    if stop_distance <= 0:
        return None
    return (exit_price - entry) / stop_distance


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=None,
                         help="ventana en días hacia atrás (default: todo el historial)")
    parser.add_argument("--csv", type=str, default=None,
                         help="ruta para exportar el detalle por señal")
    parser.add_argument("--sleep", type=float, default=0.3,
                         help="pausa entre llamadas a la MLB Stats API (seg)")
    args = parser.parse_args()

    client = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

    since_ts = None
    if args.days is not None:
        since_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - args.days * 86400))

    rows = fetch_stopped_signals(client, since_ts)
    if not rows:
        print("No hay señales con outcome='stop' en la ventana pedida.")
        return

    print(f"Resolviendo {len(rows)} señales cortadas por stop contra el resultado real del partido...\n")

    would_win_n = 0
    would_lose_n = 0
    unresolved_n = 0  # partido no encontrado como Final (raro para juegos ya viejos) o voided
    lost_r = 0.0      # suma de R que se hubiera ganado de más, solo en los casos would_win
    detail = []

    for i, sig in enumerate(rows, 1):
        game_pk = sig.get("game_pk")
        if not game_pk:
            unresolved_n += 1
            continue

        result = fetch_game_result(game_pk, timeout=DEFAULT_TIMEOUT)
        time.sleep(args.sleep)

        if result is None or result.get("voided"):
            unresolved_n += 1
            continue

        won = would_have_won(sig["direction"], result)
        r_stop = r_multiple_at_stop(sig["market_price"], sig["exit_price"], sig["stop"])
        r_final = r_multiple_at_resolution(sig["market_price"], sig["stop"], won)

        row = {
            "id": sig["id"],
            "matchup": f"{sig['away_team']} @ {sig['home_team']}",
            "direction": sig["direction"],
            "my_prob": sig["my_prob"],
            "entry": sig["market_price"],
            "stop": sig["stop"],
            "exit_price": sig["exit_price"],
            "home_score": result["home_score"],
            "away_score": result["away_score"],
            "would_have_won": won,
            "r_at_stop": round(r_stop, 2) if r_stop is not None else None,
            "r_if_held": round(r_final, 2) if r_final is not None else None,
        }
        detail.append(row)

        if won:
            would_win_n += 1
            if r_stop is not None and r_final is not None:
                lost_r += (r_final - r_stop)
            marker = "⚠ HABRÍA GANADO"
        else:
            would_lose_n += 1
            marker = "confirmado: perdía igual"

        print(f"  [{i}/{len(rows)}] #{sig['id']} {row['matchup']} "
              f"({sig['direction']}, entrada={sig['market_price']:.3f}) -> "
              f"{result['away_score']}-{result['home_score']}  {marker}")

    resolved_n = would_win_n + would_lose_n
    print("\n" + "=" * 70)
    print(f"Señales con outcome='stop' analizadas: {len(rows)}")
    print(f"  Sin game_pk / partido no resuelto todavía: {unresolved_n}")
    print(f"  Resueltas: {resolved_n}")
    if resolved_n:
        pct_would_win = 100 * would_win_n / resolved_n
        print(f"\n  Habrían GANADO si no se cortaban: {would_win_n} ({pct_would_win:.1f}%)")
        print(f"  Habrían perdido igual (el stop las salvó de una pérdida mayor): {would_lose_n} ({100-pct_would_win:.1f}%)")
        print(f"\n  R total dejado sobre la mesa en las señales que habrían ganado: {lost_r:+.2f}R")
        print(f"  (esto es la diferencia entre el R que dio el stop y el R que hubiera dado")
        print(f"   dejar correr la señal hasta la resolución real del partido, sumado solo")
        print(f"   sobre las señales marcadas ⚠ arriba)")
        if pct_would_win > 38:  # win rate real de las señales NO cortadas (44/(44+72))
            print(f"\n  El {pct_would_win:.0f}% de acierto de las señales cortadas es MAYOR al win rate")
            print(f"  real de las señales que sí llegaron a resolución (38%, 44/116) -- el stop no")
            print(f"  está filtrando señales malas, está cortando señales al azar respecto a su")
            print(f"  resultado final. Esto apunta a ruido/volatilidad normal del mercado pre-partido,")
            print(f"  no a que el modelo se equivocara.")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            writer = csv_module.DictWriter(f, fieldnames=list(detail[0].keys()) if detail else [])
            writer.writeheader()
            writer.writerows(detail)
        print(f"\nDetalle exportado a {args.csv}")


if __name__ == "__main__":
    main()
