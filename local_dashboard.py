"""
Dashboard local para el modo VPS/SQLite — el `dashboard/` de Next.js está
armado para el modo serverless (Supabase). Corriendo en local con SQLite
como estás vos, esto te da lo mismo (equity, bitácora, win rate/expectancy
real de cripto y Polymarket) sin tener que migrar nada ni instalar Node.

Server HTTP mínimo con la librería estándar — sin dependencias nuevas.

Uso:
    python local_dashboard.py                # sirve en http://localhost:8787
    python local_dashboard.py --port 9000
"""
import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from config import Config
from db import Database

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<title>Trader IA 24/7 — Dashboard local</title>
<meta http-equiv="refresh" content="15">
<style>
  body {{ font-family: 'JetBrains Mono', monospace; background:#F1ECDF; color:#181410; margin:0; padding:20px; }}
  .wrap {{ max-width:880px; margin:0 auto; }}
  h1 {{ font-size:22px; margin:0 0 16px; }}
  h2 {{ font-size:13px; margin:0 0 10px; }}
  .status {{ display:inline-block; font-size:11px; font-weight:700; text-transform:uppercase;
             padding:7px 12px; border-radius:6px; border:1.5px solid; margin-bottom:16px; }}
  .status.online {{ color:#2E6B4A; border-color:#2E6B4A; background:#DCE9DF; }}
  .status.halted {{ color:#B4392A; border-color:#B4392A; background:#F1DAD5; }}
  .card {{ background:#FBF8F1; border:1.5px solid #181410; border-radius:10px; padding:16px; margin-bottom:16px; }}
  .stats-grid {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(120px, 1fr)); gap:10px; }}
  .stat-card {{ border:1.5px solid #D9D0B9; border-radius:8px; padding:10px 12px; background:#F1ECDF; }}
  .stat-card.ok {{ border-color:#2E6B4A; background:#DCE9DF; }}
  .stat-card.fail {{ border-color:#B4392A; background:#F1DAD5; }}
  .stat-label {{ font-size:9.5px; text-transform:uppercase; color:#5C564A; margin-bottom:4px; }}
  .stat-value {{ font-size:20px; font-weight:700; }}
  table {{ width:100%; border-collapse:collapse; font-size:11.5px; }}
  th {{ text-align:left; font-size:9.5px; text-transform:uppercase; color:#5C564A; padding:6px 8px; border-bottom:1.5px solid #181410; }}
  td {{ padding:7px 8px; border-bottom:1px dashed #D9D0B9; }}
  .empty {{ color:#5C564A; font-size:12px; }}
  .ok {{ color:#2E6B4A; font-weight:700; }}
  .fail {{ color:#B4392A; font-weight:700; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>Trader IA 24/7 — Dashboard local</h1>
  <span class="status {status_class}">{status_text}</span>

  <div class="card">
    <h2>Performance — Cripto (trades cerrados)</h2>
    {crypto_stats_html}
  </div>

  <div class="card">
    <h2>Performance — Polymarket (señales resueltas)</h2>
    {polymarket_stats_html}
    {polymarket_categories_html}
  </div>

  <div class="card">
    <h2>Equity</h2>
    <div style="font-size:26px; font-weight:700;">{equity_value}</div>
  </div>

  <div class="card">
    <h2>Bitácora de decisiones (cripto)</h2>
    {decisions_html}
  </div>
</div>
</body>
</html>
"""


def stat_card(label, value, suffix="", tone=""):
    display = "—" if value is None else f"{value}{suffix}"
    cls = f"stat-card {tone}".strip()
    return f'<div class="{cls}"><div class="stat-label">{label}</div><div class="stat-value">{display}</div></div>'


def render_stats(stats, show_profit_factor=False):
    if not stats or stats.get("n", 0) == 0:
        return '<p class="empty">Sin trades cerrados todavía.</p>'
    win_rate = stats.get("win_rate")
    tone = "ok" if (win_rate or 0) >= 50 else "fail"
    cards = [
        stat_card("Trades cerrados", stats["n"]),
        stat_card("Win rate", f"{win_rate:.1f}" if win_rate is not None else None, "%", tone),
    ]
    if "expectancy_r" in stats and stats["expectancy_r"] is not None:
        exp = stats["expectancy_r"]
        cards.append(stat_card("Expectancy", f"{exp:+.2f}", "R", "ok" if exp >= 0 else "fail"))
    if show_profit_factor and stats.get("profit_factor") is not None:
        cards.append(stat_card("Profit factor", f"{stats['profit_factor']:.2f}"))
    return f'<div class="stats-grid">{"".join(cards)}</div>'


def render_polymarket_categories(by_category):
    if not by_category:
        return ""
    rows = sorted(by_category.items(), key=lambda kv: kv[1]["total_r"], reverse=True)
    body = ""
    for cat, s in rows:
        tone = "ok" if s["total_r"] >= 0 else "fail"
        pf = f"{s['profit_factor']:.2f}" if s["profit_factor"] is not None else "—"
        small_n = " ⚠️" if s["n"] < 5 else ""
        body += (
            f"<tr><td>{cat}{small_n}</td><td>{s['n']}</td><td>{s['win_rate']:.0f}%</td>"
            f"<td class='{tone}'>{s['expectancy_r']:+.2f}R</td><td>{pf}</td>"
            f"<td class='{tone}'>{s['total_r']:+.2f}R</td></tr>"
        )
    return (
        '<div style="margin-top:12px; font-size:9.5px; text-transform:uppercase; color:#5C564A;">Por categoría</div>'
        "<table><thead><tr><th>Categoría</th><th>n</th><th>Win%</th><th>Expect.</th><th>PF</th><th>Total R</th></tr></thead>"
        f"<tbody>{body}</tbody></table>"
    )


def render_decisions(rows):
    if not rows:
        return '<p class="empty">Todavía no hay decisiones registradas.</p>'
    body = ""
    for d in rows[:30]:
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(d["ts"]))
        risk_cls = "ok" if d["risk_pass"] else "fail"
        risk_txt = "OK" if d["risk_pass"] else "FALLA"
        conf = "★" * d["confidence"] if d["confidence"] else "—"
        body += (
            f"<tr><td>{ts}</td><td>{d['symbol']}</td><td>{d['signal_type'] or '—'}</td>"
            f"<td>{d['direction'] or '—'}</td><td>{conf}</td>"
            f"<td class='{risk_cls}'>{risk_txt}</td><td>{d['decision']}</td></tr>"
        )
    return (
        "<table><thead><tr><th>Fecha</th><th>Símbolo</th><th>Señal</th><th>Dirección</th>"
        "<th>Confianza</th><th>Riesgo</th><th>Decisión</th></tr></thead>"
        f"<tbody>{body}</tbody></table>"
    )


def build_page(db):
    halted = db.get_state("trading_halted", "0") == "1"
    halt_reason = db.get_state("halt_reason", "") if halted else ""
    peak = db.peak_equity()
    decisions = db.conn.execute("SELECT * FROM decisions ORDER BY ts DESC LIMIT 30").fetchall()

    return PAGE_TEMPLATE.format(
        status_class="halted" if halted else "online",
        status_text=f"Detenido — {halt_reason}" if halted else "Sistema en línea",
        crypto_stats_html=render_stats(db.stats_summary(), show_profit_factor=True),
        polymarket_stats_html=render_stats(db.polymarket_stats_summary()),
        polymarket_categories_html=render_polymarket_categories(db.polymarket_stats_by_category()),
        equity_value=f"${peak:,.2f}" if peak is not None else "—",
        decisions_html=render_decisions(decisions),
    )


def make_handler(db):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/api/data":
                payload = {
                    "halted": db.get_state("trading_halted", "0") == "1",
                    "halt_reason": db.get_state("halt_reason", ""),
                    "equity_peak": db.peak_equity(),
                    "stats": db.stats_summary(),
                    "polymarket_stats": db.polymarket_stats_summary(),
                    "polymarket_stats_by_category": db.polymarket_stats_by_category(),
                }
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)
                return

            body = build_page(db).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            pass  # silencia el log default de BaseHTTPRequestHandler

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()

    config = Config
    db = Database(config.DB_PATH)
    # HTTPServer (single-threaded) a propósito, no ThreadingHTTPServer: la
    # conexión de sqlite3 se crea en un solo thread y no es segura para
    # compartir entre threads sin check_same_thread=False — para un
    # dashboard de una sola persona no hace falta manejar requests en
    # paralelo, así que evitamos ese problema directamente en vez de tocar
    # la semántica de conexión compartida de db.py.
    server = HTTPServer(("0.0.0.0", args.port), make_handler(db))
    print(f"Dashboard local en http://localhost:{args.port} (Ctrl+C para salir)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
