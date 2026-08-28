"""
Resumen semanal de performance real (no backtest) por Telegram: win rate,
expectancy en R y mejor/peor símbolo sobre los trades cerrados en los
últimos 7 días.

Antes, la única forma de ver cómo venía el sistema era entrar al dashboard
o leer la bitácora cruda — esto manda un mensaje corto y accionable directo
a Telegram, que es donde ya estás mirando las alertas.

Uso:
    python weekly_summary.py                 # manda el resumen ahora
    python weekly_summary.py --days 30        # ventana de 30 días en vez de 7

Para que se mande solo, agregalo a un cron (VPS) o a una tarea programada:
    0 9 * * 1 cd /ruta/al/repo && venv/bin/python weekly_summary.py
(todos los lunes a las 9am, resume la semana anterior)
"""
import argparse
import time

from config import Config
from db import Database
from telegram_notifier import TelegramNotifier


def build_summary_text(config, db, days):
    since_ts = time.time() - days * 86400
    overall = db.stats_summary(since_ts=since_ts)
    by_symbol = db.stats_by_symbol(since_ts=since_ts)

    lines = [f"📊 *Resumen — últimos {days} días*", ""]

    if overall["n"] == 0:
        lines.append("Sin trades cerrados en este período todavía.")
        return "\n".join(lines)

    lines.append(f"Trades cerrados: {overall['n']}")
    lines.append(f"Win rate: {overall['win_rate']:.1f}%")
    lines.append(f"Expectancy: {overall['expectancy_r']:+.2f}R por trade")
    if overall["profit_factor"] is not None:
        lines.append(f"Profit factor: {overall['profit_factor']:.2f}")

    if by_symbol:
        ranked = sorted(by_symbol.items(), key=lambda kv: kv[1]["total_r"], reverse=True)
        lines.append("")
        lines.append("Por símbolo (total R):")
        for symbol, s in ranked:
            lines.append(f"  {symbol}: {s['n']} trades · {s['win_rate']:.0f}% win · {s['total_r']:+.2f}R total")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7, help="ventana en días (default: 7)")
    parser.add_argument("--dry-run", action="store_true", help="imprime el resumen sin mandarlo a Telegram")
    args = parser.parse_args()

    config = Config
    db = Database(config.DB_PATH)
    text = build_summary_text(config, db, args.days)

    print(text.replace("*", ""))

    if args.dry_run:
        return

    notifier = TelegramNotifier(config)
    if notifier.enabled:
        notifier.send_message(text)
    else:
        print("\n(NOTIFY_TELEGRAM no está activo — solo se mostró en consola.)")


if __name__ == "__main__":
    main()
