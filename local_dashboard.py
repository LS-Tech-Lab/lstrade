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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
  .delta-up {{ color:#2E6B4A; }}
  .delta-down {{ color:#B4392A; }}
  .delta-flat {{ color:#5C564A; }}
  .sparkline-wrap {{ overflow-x:auto; }}
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
  </div>

  <div class="card">
    <h2>Equity</h2>
    <div style="display:flex; align-items:baseline; gap:10px; margin-bottom:10px;">
      <div style="font-size:26px; font-weight:700;">{equity_value}</div>
      <div style="font-size:12.5px; font-weight:700;" class="{equity_delta_class}">{equity_delta_text}</div>
    </div>
    {equity_sparkline_html}
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


def render_equity_sparkline(rows):
    """
    rows: lista de (ts, equity) ordenada cronológicamente, desde equity_history.
    Genera un SVG inline a mano (sin matplotlib ni JS) con la curva completa
    y resalta el último punto — antes esta tabla existía en la base pero el
    dashboard solo mostraba el pico como número suelto, sin mostrar la
    trayectoria real de la cuenta.
    """
    if not rows or len(rows) < 2:
        return '<p class="empty">Todavía no hay suficiente historial de equity para graficar.</p>'

    values = [r["equity"] for r in rows]
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0

    w, h, pad = 760, 90, 6
    n = len(values)
    step = (w - 2 * pad) / (n - 1)

    def point(i, v):
        x = pad + i * step
        y = h - pad - ((v - lo) / span) * (h - 2 * pad)
        return x, y

    pts = [point(i, v) for i, v in enumerate(values)]
    path_d = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in pts)

    trend_up = values[-1] >= values[0]
    line_color = "#2E6B4A" if trend_up else "#B4392A"
    fill_d = path_d + f" L {pts[-1][0]:.1f},{h - pad} L {pts[0][0]:.1f},{h - pad} Z"
    last_x, last_y = pts[-1]

    return f"""<div class="sparkline-wrap">
<svg viewBox="0 0 {w} {h}" width="100%" height="{h}" preserveAspectRatio="none">
  <path d="{fill_d}" fill="{line_color}" fill-opacity="0.08" stroke="none"/>
  <path d="{path_d}" fill="none" stroke="{line_color}" stroke-width="2" vector-effect="non-scaling-stroke"/>
  <circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="3.5" fill="{line_color}"/>
</svg>
</div>"""


def equity_delta(rows):
    if not rows or len(rows) < 2:
        return "", ""
    first, last = rows[0]["equity"], rows[-1]["equity"]
    diff = last - first
    pct = (diff / first * 100) if first else 0
    if diff > 0:
        return "delta-up", f"▲ {diff:+,.2f} ({pct:+.1f}%) en este historial"
    if diff < 0:
        return "delta-down", f"▼ {diff:+,.2f} ({pct:+.1f}%) en este historial"
    return "delta-flat", "sin cambio en este historial"


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
    equity_rows = db.conn.execute(
        "SELECT ts, equity FROM equity_history ORDER BY ts ASC LIMIT 200"
    ).fetchall()
    delta_class, delta_text = equity_delta(equity_rows)

    return PAGE_TEMPLATE.format(
        status_class="halted" if halted else "online",
        status_text=f"Detenido — {halt_reason}" if halted else "Sistema en línea",
        crypto_stats_html=render_stats(db.stats_summary(), show_profit_factor=True),
        polymarket_stats_html=render_stats(db.polymarket_stats_summary()),
        equity_value=f"${peak:,.2f}" if peak is not None else "—",
        equity_delta_class=delta_class,
        equity_delta_text=delta_text,
        equity_sparkline_html=render_equity_sparkline(equity_rows),
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
    server = ThreadingHTTPServer(("0.0.0.0", args.port), make_handler(db))
    print(f"Dashboard local en http://localhost:{args.port} (Ctrl+C para salir)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()