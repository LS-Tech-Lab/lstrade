"""
Modo manual del análisis de clima — reporte completo para correr vos mismo
antes de decidir una operación, sin tocar el ciclo automático (que vive en
/api/weather_cycle dentro de app.py y solo usa fuentes oficiales con API estable).

Uso:
    python weather_report.py                    # analiza todos los eventos
                                                  # de clima activos en Polymarket
    python weather_report.py --search miami      # filtra por texto en el
                                                  # título del evento
    python weather_report.py --station KMIA      # fuerza una estación ICAO
                                                  # en vez de resolverla del
                                                  # título
    python weather_report.py --notify            # además de imprimir en
                                                  # consola, manda el reporte
                                                  # compacto por Telegram

Este script usa las mismas fuentes oficiales que el ciclo automático (NWS +
METAR/TAF). NO incluye Weather Underground, AccuWeather ni NOAA timeseries
— esas no tienen API pública gratuita y confiable para hardcodear acá. Para
el cruce multi-fuente completo que describe la skill `wu-airport-weather`
original (WU, AccuWeather, NOAA), pedile directamente a Claude en una
conversación que corra esa skill sobre el mismo mercado — la salida de este
script (estación resuelta, estimación de NWS, distribución de buckets) es
un buen punto de partida para pegarle a Claude y que cruce con esas fuentes
adicionales antes de que decidas.
"""
import argparse
import logging

from config import Config
from polymarket_client import PolymarketClient
from telegram_notifier import TelegramNotifier
from weather_signal_engine import (
    generate_weather_signal,
    build_weather_memo,
    resolve_station,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("weather_report")


def print_full_report(signal):
    """Reporte en consola siguiendo el formato STEP 4 de la skill
    wu-airport-weather (Settlement Check, Station State, Bucket
    Distribution, Physics Case, Trade View) — más detallado que el memo
    compacto de Telegram."""
    print("\n" + "=" * 78)

    if signal["status"] == "no_station":
        print(f"🌡️ {signal['title']}")
        print("⚖️ SETTLEMENT CHECK: estación NO resuelta automáticamente.")
        print("   Agregá la ciudad a STATION_MAP en weather_signal_engine.py")
        print("   o corré con --station <ICAO>.")
        print("=" * 78)
        return

    if signal["status"] in ("no_buckets", "no_data"):
        st = signal.get("station", {})
        print(f"🌡️ {signal['title']}")
        print(f"⚖️ Estación: {st.get('name', st.get('icao', '?'))}")
        reason = "no se pudo parsear ningún bucket de temperatura de este evento" \
            if signal["status"] == "no_buckets" else \
            "no hay ninguna fuente de guía disponible (NWS y METAR fallaron)"
        print(f"❌ Sin análisis posible: {reason}.")
        print("=" * 78)
        return

    st = signal["station"]
    print(f"🌡️  WEATHER MARKET ANALYSIS — {signal['title']}")
    print(f"    Estimación de máxima: {signal['center_estimate_f']}°F  (σ={signal['sigma']:.1f}°F, penalización de confianza {signal['confidence_penalty']:.2f})")

    print("\n⚖️  SETTLEMENT CHECK")
    verified = "✅ verificada" if signal["settlement_verified"] else "⚠️  SIN VERIFICAR"
    print(f"    Estación: {st.get('name', st.get('icao'))} ({st.get('icao')}) — {verified}")
    if st.get("note"):
        print(f"    Nota: {st['note']}")

    print("\n📡  STATION STATE")
    nws = signal.get("nws")
    metar = signal.get("metar")
    taf = signal.get("taf")
    if nws:
        print(f"    NWS: máxima pronosticada {nws.get('forecast_high_f')}°F — {nws.get('short_forecast')}  (actualizado {nws.get('issued')})")
    else:
        print("    NWS: sin datos (fetch falló o timeout)")
    if metar:
        max6 = metar.get("six_hr_max_f")
        max6_txt = f"{max6:.1f}°F" if max6 is not None else "no reportado en remarks"
        temp_txt = f"{metar.get('temp_f'):.1f}°F" if metar.get("temp_f") is not None else "N/A"
        print(f"    METAR ({metar.get('obs_time')}): {temp_txt} | máxima de 6h: {max6_txt}")
        print(f"    RAW: {metar.get('raw')}")
    else:
        print("    METAR: sin datos (fetch falló o timeout)")
    if taf:
        storm_txt = "SÍ — hay señal de tormenta/chubascos en el TAF" if taf.get("storm_signal") else "no"
        print(f"    TAF: señal de tormenta antes del pico de calor: {storm_txt}")
    else:
        print("    TAF: sin datos (fetch falló o timeout)")

    print("\n📊  BUCKET DISTRIBUTION (mi estimación vs. mercado)")
    print(f"    {'Bucket':<45} {'Mi prob':>9} {'Precio mkt':>11} {'EV/$1':>9}")
    for row in signal["buckets"]:
        ev_txt = f"{row['ev']*100:+.1f}%" if row["ev"] is not None else "N/A"
        print(f"    {row['question'][:45]:<45} {row['my_prob']*100:>8.1f}% {row['market_price']:>10.3f} {ev_txt:>9}")

    print("\n🌦️  PHYSICS CASE")
    for note in signal["physics_notes"]:
        print(f"    • {note}")

    print("\n🎯  TRADE VIEW")
    if signal["best_trade"]:
        bt = signal["best_trade"]
        print(f"    Mejor EV: {bt['question']}")
        print(f"    EV {bt['ev']*100:+.1f}% @ ${bt['market_price']:.3f} | mi prob {bt['my_prob']*100:.1f}% | liquidez ${bt['liquidity']:,.0f}")
    else:
        print("    Sin edge suficiente hoy — pasar es la disciplina correcta.")

    print(f"\n⚠️  {signal['disclaimer']}")
    print("=" * 78)


def main():
    parser = argparse.ArgumentParser(description="Reporte manual de análisis de clima para Polymarket")
    parser.add_argument("--search", type=str, default=None, help="filtra eventos por texto en el título (ej: 'miami')")
    parser.add_argument("--station", type=str, default=None, help="fuerza una estación ICAO (ej: KMIA) en vez de resolverla del título")
    parser.add_argument("--min-ev", type=float, default=None, help="umbral de EV mínimo (default: Config.WEATHER_MIN_EV)")
    parser.add_argument("--notify", action="store_true", help="además de imprimir, manda el reporte compacto por Telegram")
    parser.add_argument("--limit", type=int, default=20, help="cuántos eventos de clima traer de Polymarket (default: 20)")
    args = parser.parse_args()

    config = Config
    if args.station:
        config.WEATHER_STATION_OVERRIDE = args.station

    client = PolymarketClient(config)
    notifier = TelegramNotifier(config) if args.notify else None

    log.info("Buscando eventos de clima activos en Polymarket...")
    events = client.fetch_weather_events(limit=args.limit)

    if args.search:
        needle = args.search.lower()
        events = [e for e in events if needle in e["title"].lower()]

    if not events:
        print("No se encontraron eventos de clima" + (f" que matcheen '{args.search}'." if args.search else "."))
        print("Tip: si el mercado que buscás usa una ciudad que no está en")
        print("STATION_MAP, corré con --station <ICAO> para forzarla.")
        return

    log.info(f"{len(events)} evento(s) de clima encontrados. Analizando...")

    min_ev = args.min_ev if args.min_ev is not None else config.WEATHER_MIN_EV

    for event in events:
        try:
            signal = generate_weather_signal(event, config, min_ev=min_ev)
        except Exception as e:
            log.exception(f"Error analizando '{event['title']}': {e}")
            continue

        print_full_report(signal)

        if args.notify and signal.get("status") == "ok":
            memo = build_weather_memo(signal, markdown=True)
            if memo:
                notifier.send_message(memo)
                log.info(f"Reporte enviado a Telegram: {event['title'][:50]}")

    print("\n" + "-" * 78)
    print("Este reporte usa solo fuentes oficiales (NWS + METAR/TAF).")
    print("Para el cruce completo con Weather Underground, AccuWeather y NOAA")
    print("timeseries (como describe la skill wu-airport-weather), pegale este")
    print("reporte a Claude en una conversación y pedile que corra esa skill")
    print("sobre el mismo mercado antes de decidir.")
    print("-" * 78)


if __name__ == "__main__":
    main()
