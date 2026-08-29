"use client";
import { useEffect, useState } from "react";

function EquitySparkline({ points }) {
  if (!points || points.length < 2) {
    return <div className="empty">Todavía no hay suficiente historial de equity.</div>;
  }
  const values = points.map((p) => p.equity);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const w = 600, h = 120, pad = 8;
  const norm = points
    .map((p, i) => {
      const x = pad + (i / (points.length - 1)) * (w - pad * 2);
      const y = h - pad - ((p.equity - min) / (max - min || 1)) * (h - pad * 2);
      return `${x},${y}`;
    })
    .join(" ");
  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="equity-chart" preserveAspectRatio="none">
      <polyline points={norm} fill="none" stroke="#E15A2C" strokeWidth="2" />
    </svg>
  );
}

function StatCard({ label, value, suffix = "", tone }) {
  return (
    <div className={`stat-card ${tone || ""}`}>
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value === null || value === undefined ? "—" : `${value}${suffix}`}</div>
    </div>
  );
}

function StatsRow({ title, stats, showProfitFactor }) {
  if (!stats || stats.n === 0) {
    return (
      <div className="card">
        <h2>{title}</h2>
        <p className="empty">Sin trades cerrados todavía — las métricas aparecen cuando haya resultados reales.</p>
      </div>
    );
  }
  const winTone = stats.win_rate >= 50 ? "ok" : "fail";
  return (
    <div className="card">
      <h2>{title}</h2>
      <div className="stats-grid">
        <StatCard label="Trades cerrados" value={stats.n} />
        <StatCard label="Win rate" value={stats.win_rate?.toFixed(1)} suffix="%" tone={winTone} />
        {stats.expectancy_r !== undefined && (
          <StatCard label="Expectancy" value={stats.expectancy_r >= 0 ? `+${stats.expectancy_r.toFixed(2)}` : stats.expectancy_r.toFixed(2)} suffix="R" tone={stats.expectancy_r >= 0 ? "ok" : "fail"} />
        )}
        {showProfitFactor && stats.profit_factor !== null && stats.profit_factor !== undefined && (
          <StatCard label="Profit factor" value={stats.profit_factor.toFixed(2)} />
        )}
      </div>
    </div>
  );
}

function Tabs({ active, onChange }) {
  return (
    <div className="tabs">
      <button className={`tab ${active === "cripto" ? "active" : ""}`} onClick={() => onChange("cripto")}>
        Cripto
      </button>
      <button className={`tab ${active === "polymarket" ? "active" : ""}`} onClick={() => onChange("polymarket")}>
        Polymarket
      </button>
    </div>
  );
}

// Barra de RSI: verde en zona neutral, ámbar/rojo en sobrecompra/sobreventa —
// mismos umbrales que signal_engine.py (72 sobrecompra, 28 sobreventa) para
// que lo que se ve acá coincida con lo que el bot realmente filtra.
function RsiBar({ value }) {
  if (value === null || value === undefined) return <div className="empty">Sin datos de RSI todavía.</div>;
  const pct = Math.max(0, Math.min(100, value));
  let tone = "ok";
  if (value > 72 || value < 28) tone = "fail";
  else if (value > 65 || value < 35) tone = "warn";
  return (
    <div className="rsi-bar-wrap">
      <div className="rsi-bar-track">
        <div className={`rsi-bar-fill ${tone}`} style={{ width: `${pct}%` }} />
        <div className="rsi-bar-marker" style={{ left: "28%" }} />
        <div className="rsi-bar-marker" style={{ left: "72%" }} />
      </div>
      <div className={`rsi-bar-value ${tone}`}>{value.toFixed(1)}</div>
    </div>
  );
}

function IndicatorCard({ symbol, snapshot }) {
  if (!snapshot) {
    return (
      <div className="indicator-card">
        <div className="indicator-symbol">{symbol}</div>
        <p className="empty">Todavía no hay snapshot — aparece en el próximo ciclo.</p>
      </div>
    );
  }
  const biasTone = snapshot.trend_bias === "LONG" ? "ok" : "fail";
  return (
    <div className="indicator-card">
      <div className="indicator-header">
        <div className="indicator-symbol">{symbol}</div>
        <span className={`bias-badge ${biasTone}`}>{snapshot.trend_bias}</span>
      </div>
      <div className="indicator-price">
        {snapshot.price !== null ? `$${Number(snapshot.price).toLocaleString(undefined, { maximumFractionDigits: 6 })}` : "—"}
      </div>
      <div className="indicator-row">
        <span className="label">RSI (14)</span>
        <RsiBar value={snapshot.rsi} />
      </div>
      <div className="indicator-mini-grid">
        <div>
          <span className="label">Momentum</span>
          <div className={snapshot.momentum >= 0 ? "ok" : "fail"}>
            {snapshot.momentum !== null ? `${(snapshot.momentum * 100).toFixed(2)}%` : "—"}
          </div>
        </div>
        <div>
          <span className="label">Volatilidad</span>
          <div>{snapshot.volatility !== null ? `${(snapshot.volatility * 100).toFixed(2)}%` : "—"}</div>
        </div>
        <div>
          <span className="label">ATR</span>
          <div>{snapshot.atr_pct !== null ? `${snapshot.atr_pct.toFixed(2)}%` : "—"}</div>
        </div>
        <div>
          <span className="label">Vol. ratio</span>
          <div>{snapshot.volume_ratio !== null ? `${snapshot.volume_ratio.toFixed(2)}x` : "—"}</div>
        </div>
      </div>
      <div className="indicator-ts">Actualizado: {new Date(snapshot.ts * 1000).toLocaleTimeString()}</div>
    </div>
  );
}

function PolymarketCategoryTable({ byCategory }) {
  const rows = Object.entries(byCategory || {}).sort((a, b) => b[1].total_r - a[1].total_r);
  if (rows.length === 0) {
    return <p className="empty">Todavía no hay señales resueltas para desglosar por categoría.</p>;
  }
  return (
    <table>
      <thead>
        <tr>
          <th>Categoría</th>
          <th>n</th>
          <th>Win%</th>
          <th>Expectancy</th>
          <th>PF</th>
          <th>Total R</th>
        </tr>
      </thead>
      <tbody>
        {rows.map(([cat, s]) => {
          const tone = s.total_r >= 0 ? "ok" : "fail";
          return (
            <tr key={cat}>
              <td>{cat}{s.n < 5 ? " ⚠️" : ""}</td>
              <td>{s.n}</td>
              <td>{s.win_rate.toFixed(0)}%</td>
              <td className={tone}>{s.expectancy_r >= 0 ? "+" : ""}{s.expectancy_r.toFixed(2)}R</td>
              <td>{s.profit_factor !== null ? s.profit_factor.toFixed(2) : "—"}</td>
              <td className={tone}>{s.total_r >= 0 ? "+" : ""}{s.total_r.toFixed(2)}R</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function PolymarketOpenTable({ rows }) {
  if (!rows || rows.length === 0) {
    return <p className="empty">Sin señales de Polymarket abiertas ahora mismo.</p>;
  }
  return (
    <table>
      <thead>
        <tr>
          <th>Mercado</th>
          <th>Dirección</th>
          <th>Entrada</th>
          <th>Target</th>
          <th>Stop</th>
          <th>Enviada</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.id}>
            <td>{r.question?.length > 60 ? `${r.question.slice(0, 60)}…` : r.question}</td>
            <td>{r.direction}</td>
            <td>{r.entry?.toFixed(3)}</td>
            <td>{r.target?.toFixed(3)}</td>
            <td>{r.stop?.toFixed(3)}</td>
            <td>{new Date(r.ts_signaled * 1000).toLocaleString()}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function PolymarketResolvedTable({ rows }) {
  if (!rows || rows.length === 0) {
    return <p className="empty">Todavía no hay señales resueltas.</p>;
  }
  return (
    <table>
      <thead>
        <tr>
          <th>Mercado</th>
          <th>Dirección</th>
          <th>Resultado</th>
          <th>Resuelta</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.id}>
            <td>{r.question?.length > 60 ? `${r.question.slice(0, 60)}…` : r.question}</td>
            <td>{r.direction}</td>
            <td className={r.outcome === "target" ? "ok" : "fail"}>{r.outcome === "target" ? "GANADA" : "PERDIDA"}</td>
            <td>{r.ts_resolved ? new Date(r.ts_resolved * 1000).toLocaleString() : "—"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function CriptoTab({ data }) {
  const lastEquity = data.equity.length ? data.equity[data.equity.length - 1].equity : null;
  const indicatorsBySymbol = Object.fromEntries((data.indicators || []).map((s) => [s.symbol, s]));
  // Símbolos a mostrar: unión de lo configurado (inferido de los snapshots
  // recibidos) y lo que aparece en la bitácora, así no queda un símbolo
  // fuera solo porque nunca tuvo señal de trading.
  const symbols = Array.from(
    new Set([...(data.indicators || []).map((s) => s.symbol), ...data.decisions.map((d) => d.symbol)])
  );

  return (
    <>
      {symbols.length > 0 && (
        <div className="card">
          <h2>Indicadores en vivo</h2>
          <div className="indicator-grid">
            {symbols.map((symbol) => (
              <IndicatorCard key={symbol} symbol={symbol} snapshot={indicatorsBySymbol[symbol]} />
            ))}
          </div>
        </div>
      )}

      <StatsRow title="Performance — Cripto (trades cerrados)" stats={data.stats} showProfitFactor />

      <div className="card">
        <h2>Equity</h2>
        {lastEquity !== null && <div className="equity-value">${lastEquity.toFixed(2)}</div>}
        <EquitySparkline points={data.equity} />
      </div>

      <div className="card">
        <h2>Bitácora de decisiones</h2>
        {data.decisions.length === 0 ? (
          <p className="empty">Todavía no hay decisiones registradas.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Fecha</th>
                <th>Símbolo</th>
                <th>Señal</th>
                <th>Dirección</th>
                <th>Confianza</th>
                <th>Riesgo</th>
                <th>Decisión</th>
              </tr>
            </thead>
            <tbody>
              {data.decisions.map((d) => (
                <tr key={d.id}>
                  <td>{new Date(d.ts * 1000).toLocaleString()}</td>
                  <td>{d.symbol}</td>
                  <td>{d.signal_type || "—"}</td>
                  <td>{d.direction || "—"}</td>
                  <td>{d.confidence ? "★".repeat(d.confidence) : "—"}</td>
                  <td className={d.risk_pass ? "ok" : "fail"}>{d.risk_pass ? "OK" : "FALLA"}</td>
                  <td className={`decision-${d.decision}`}>{d.decision}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}

function PolymarketTab({ data }) {
  return (
    <>
      <StatsRow title="Performance — Polymarket (señales resueltas)" stats={data.polymarket_stats} />

      <div className="card">
        <h2>Performance por categoría</h2>
        <PolymarketCategoryTable byCategory={data.polymarket_stats_by_category} />
      </div>

      <div className="card">
        <h2>Señales abiertas ({data.polymarket_open?.length || 0})</h2>
        <PolymarketOpenTable rows={data.polymarket_open} />
      </div>

      <div className="card">
        <h2>Historial reciente</h2>
        <PolymarketResolvedTable rows={data.polymarket_resolved} />
      </div>
    </>
  );
}

export default function Dashboard() {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [tab, setTab] = useState("cripto");

  async function load() {
    try {
      const res = await fetch("/api/data", { cache: "no-store" });
      const json = await res.json();
      if (!res.ok) throw new Error(json.error || "No se pudo cargar el panel");
      setData(json);
      setError("");
    } catch (e) {
      setError(e.message);
    }
  }

  useEffect(() => {
    load();
    const id = setInterval(load, 15000); // refresco cada 15s
    return () => clearInterval(id);
  }, []);

  if (error) {
    return (
      <div className="wrap">
        <p className="error">{error}</p>
      </div>
    );
  }
  if (!data) {
    return (
      <div className="wrap">
        <p className="label">Cargando…</p>
      </div>
    );
  }

  return (
    <div className="wrap">
      <div className="header">
        <h1>Trader IA 24/7</h1>
        <span className={`status ${data.halted ? "halted" : "online"}`}>
          {data.halted ? `Detenido — ${data.halt_reason}` : "Sistema en línea"}
        </span>
      </div>

      {data.pending.length > 0 && (
        <div className="pending-banner">
          Esperando tu respuesta en Telegram para: {data.pending.map((p) => p.symbol).join(", ")}
        </div>
      )}

      <Tabs active={tab} onChange={setTab} />

      {tab === "cripto" ? <CriptoTab data={data} /> : <PolymarketTab data={data} />}
    </div>
  );
}
