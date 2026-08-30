"use client";
import { useEffect, useState } from "react";

// ────────────────────────────────────────────────────────────────────
// Diccionario en español simple. Centraliza las explicaciones de los
// términos técnicos (RSI, expectancy, profit factor, etc.) para que no
// queden regadas por todo el archivo, y para que alguien sin experiencia
// en trading pueda entender cada número sin tener que buscarlo aparte.
// ────────────────────────────────────────────────────────────────────
const GLOSSARY = [
  ["Win rate", "De cada 100 operaciones cerradas, cuántas terminaron ganando."],
  ["Expectancy (R)", "Ganancia o pérdida promedio por operación, medida en \"múltiplos de riesgo\" (R). +0.30R significa que, en promedio, cada operación gana un 30% de lo que se arriesgó en ella."],
  ["Profit factor", "Cuánto se ganó por cada $1 que se perdió. Un valor de 1.50 significa que por cada dólar perdido se ganaron $1.50. Por debajo de 1.0 el sistema pierde dinero en conjunto."],
  ["RSI", "Mide si un precio subió o bajó demasiado rápido en los últimos períodos. Arriba de 72 se considera \"sobrecomprado\" (riesgo de corrección a la baja); debajo de 28, \"sobrevendido\" (riesgo de rebote al alza)."],
  ["Momentum", "Cuánto cambió el precio en las últimas velas. Positivo = viene subiendo, negativo = viene bajando."],
  ["Volatilidad", "Qué tan bruscos son los movimientos de precio recientes. Más alto = movimientos más erráticos."],
  ["ATR", "Rango de movimiento típico de cada vela, en porcentaje del precio. Sirve como referencia de qué tan \"ancho\" se mueve el mercado ahora mismo."],
  ["Vol. ratio", "Actividad de compra/venta comparada con lo normal. 1.0x = actividad normal, 2.0x = el doble de lo habitual."],
  ["Tendencia (bias)", "Hacia dónde apunta el precio en el mediano plazo, comparando dos promedios móviles. Alcista = viene subiendo, bajista = viene bajando."],
];

// Traduce los códigos internos (los mismos que usa el motor en Python) a
// una frase corta y clara — evita que alguien sin contexto tenga que
// adivinar qué significa "paper_logged_no_telegram".
function decisionLabel(code) {
  const map = {
    auto_executed: "Ejecutada automáticamente",
    approved: "Aprobada por vos",
    rejected: "Rechazada por vos",
    blocked: "Bloqueada por riesgo",
    paper_logged: "Registrada (modo papel)",
    paper_logged_no_telegram: "Registrada (papel, sin Telegram)",
    pending_approval: "Esperando tu aprobación",
    watchlist: "En observación",
  };
  return map[code] || code;
}

function directionLabel(dir) {
  if (dir === "LONG") return "Compra";
  if (dir === "SHORT") return "Venta";
  return dir || "—";
}

// Estado en palabras simples para un valor de RSI — mismos umbrales que
// signal_engine.py (78 sobrecompra, 22 sobreventa) para que lo que se ve
// acá coincida con lo que el bot realmente filtra.
function rsiState(value) {
  if (value === null || value === undefined) return { text: "Sin datos", tone: "" };
  if (value > 78) return { text: "Sobrecomprado — riesgo de corrección", tone: "fail" };
  if (value < 22) return { text: "Sobrevendido — riesgo de rebote", tone: "fail" };
  if (value > 65 || value < 35) return { text: "Acercándose al extremo", tone: "warn" };
  return { text: "En rango neutral", tone: "ok" };
}

// Insignia "(?)" con una explicación corta al pasar el mouse o al tocarla
// en pantallas táctiles (usa :focus además de :hover, ver globals.css).
function Info({ text }) {
  return (
    <span className="info-badge" tabIndex={0} role="note" aria-label={text}>
      ?<span className="info-tooltip">{text}</span>
    </span>
  );
}

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

function StatCard({ label, value, suffix = "", tone, info }) {
  return (
    <div className={`stat-card ${tone || ""}`}>
      <div className="stat-label">
        {label}
        {info && <Info text={info} />}
      </div>
      <div className="stat-value">{value === null || value === undefined ? "—" : `${value}${suffix}`}</div>
    </div>
  );
}

function StatsRow({ title, subtitle, stats, showProfitFactor, emptyMessage }) {
  if (!stats || stats.n === 0) {
    return (
      <div className="card">
        <h2>{title}</h2>
        <p className="empty">{emptyMessage || "Sin trades cerrados todavía — las métricas aparecen cuando haya resultados reales."}</p>
      </div>
    );
  }
  const winTone = stats.win_rate >= 50 ? "ok" : "fail";
  return (
    <div className="card">
      <h2>{title}</h2>
      {subtitle && <p className="card-subtitle">{subtitle}</p>}
      <div className="stats-grid">
        <StatCard label="Trades cerrados" value={stats.n} />
        <StatCard label="Win rate" value={stats.win_rate?.toFixed(1)} suffix="%" tone={winTone}
          info={GLOSSARY.find(([k]) => k === "Win rate")[1]} />
        {stats.expectancy_r !== undefined && (
          <StatCard label="Expectancy" value={stats.expectancy_r >= 0 ? `+${stats.expectancy_r.toFixed(2)}` : stats.expectancy_r.toFixed(2)} suffix="R"
            tone={stats.expectancy_r >= 0 ? "ok" : "fail"} info={GLOSSARY.find(([k]) => k === "Expectancy (R)")[1]} />
        )}
        {showProfitFactor && stats.profit_factor !== null && stats.profit_factor !== undefined && (
          <StatCard label="Profit factor" value={stats.profit_factor.toFixed(2)} info={GLOSSARY.find(([k]) => k === "Profit factor")[1]} />
        )}
      </div>
    </div>
  );
}

// Resumen en una sola frase, en español llano — pensado para alguien que
// abre el panel por primera vez y solo quiere saber "¿cómo va esto?" sin
// tener que interpretar cada número por separado.
function PlainSummary({ halted, haltReason, stats, label }) {
  let text;
  let tone = "";
  if (halted) {
    text = `El bot está detenido${haltReason ? ` (motivo: ${haltReason})` : ""}. No va a abrir operaciones nuevas hasta que se reactive.`;
    tone = "fail";
  } else if (!stats || stats.n === 0) {
    text = `Todavía no hay operaciones de ${label} cerradas — el desempeño real se podrá evaluar apenas se cierre la primera.`;
  } else {
    const positive = stats.expectancy_r === undefined || stats.expectancy_r === null || stats.expectancy_r >= 0;
    text = `De ${stats.n} operaciones de ${label} cerradas, el ${stats.win_rate.toFixed(0)}% fueron ganadoras. `
      + (stats.expectancy_r !== undefined && stats.expectancy_r !== null
        ? (positive
          ? `En promedio, el sistema está ganando por operación.`
          : `En promedio, el sistema está perdiendo por operación — vale la pena revisar la estrategia.`)
        : "");
    tone = positive ? "ok" : "fail";
  }
  return <div className={`plain-summary ${tone}`}>{text}</div>;
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

function RsiBar({ value }) {
  if (value === null || value === undefined) return <div className="empty">Sin datos de RSI todavía.</div>;
  const pct = Math.max(0, Math.min(100, value));
  const state = rsiState(value);
  return (
    <div className="rsi-block">
      <div className="rsi-bar-wrap">
        <div className="rsi-bar-track">
          <div className={`rsi-bar-fill ${state.tone}`} style={{ width: `${pct}%` }} />
          <div className="rsi-bar-marker" style={{ left: "22%" }} />
          <div className="rsi-bar-marker" style={{ left: "78%" }} />
        </div>
        <div className={`rsi-bar-value ${state.tone}`}>{value.toFixed(1)}</div>
      </div>
      <div className={`rsi-state ${state.tone}`}>{state.text}</div>
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
  const isLong = snapshot.trend_bias === "LONG";
  const biasTone = isLong ? "ok" : "fail";
  return (
    <div className="indicator-card">
      <div className="indicator-header">
        <div className="indicator-symbol">{symbol}</div>
        <span className={`bias-badge ${biasTone}`}>{isLong ? "▲ Alcista" : "▼ Bajista"}</span>
      </div>
      <div className="indicator-price">
        {snapshot.price !== null ? `$${Number(snapshot.price).toLocaleString(undefined, { maximumFractionDigits: 6 })}` : "—"}
      </div>
      <div className="indicator-row">
        <span className="label">RSI (14) <Info text={GLOSSARY.find(([k]) => k === "RSI")[1]} /></span>
        <RsiBar value={snapshot.rsi} />
      </div>
      <div className="indicator-mini-grid">
        <div>
          <span className="label">Momentum <Info text={GLOSSARY.find(([k]) => k === "Momentum")[1]} /></span>
          <div className={snapshot.momentum >= 0 ? "ok" : "fail"}>
            {snapshot.momentum !== null ? `${(snapshot.momentum * 100).toFixed(2)}%` : "—"}
          </div>
        </div>
        <div>
          <span className="label">Volatilidad <Info text={GLOSSARY.find(([k]) => k === "Volatilidad")[1]} /></span>
          <div>{snapshot.volatility !== null ? `${(snapshot.volatility * 100).toFixed(2)}%` : "—"}</div>
        </div>
        <div>
          <span className="label">ATR <Info text={GLOSSARY.find(([k]) => k === "ATR")[1]} /></span>
          <div>{snapshot.atr_pct !== null ? `${snapshot.atr_pct.toFixed(2)}%` : "—"}</div>
        </div>
        <div>
          <span className="label">Vol. ratio <Info text={GLOSSARY.find(([k]) => k === "Vol. ratio")[1]} /></span>
          <div>{snapshot.volume_ratio !== null ? `${snapshot.volume_ratio.toFixed(2)}x` : "—"}</div>
        </div>
      </div>
      <div className="indicator-ts">Actualizado: {new Date(snapshot.ts * 1000).toLocaleTimeString()}</div>
    </div>
  );
}

function CryptoOpenTable({ rows }) {
  if (!rows || rows.length === 0) {
    return <p className="empty">Sin posiciones cripto abiertas ahora mismo.</p>;
  }
  return (
    <table>
      <thead>
        <tr>
          <th>Símbolo</th>
          <th>Dirección</th>
          <th>Entrada</th>
          <th>Target</th>
          <th>Stop</th>
          <th>Tamaño</th>
          <th>Abierta</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.id}>
            <td>{r.symbol}</td>
            <td>{directionLabel(r.direction)}</td>
            <td>{r.entry_price?.toFixed(6)}</td>
            <td>{r.target_price?.toFixed(6)}</td>
            <td>{r.current_stop?.toFixed(6)}</td>
            <td>{r.position_size?.toFixed(6)}</td>
            <td>{new Date(r.ts_opened * 1000).toLocaleString()}</td>
          </tr>
        ))}
      </tbody>
    </table>
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
            <td>{directionLabel(r.direction)}</td>
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
            <td>{directionLabel(r.direction)}</td>
            <td className={r.outcome === "target" ? "ok" : "fail"}>{r.outcome === "target" ? "GANADA" : "PERDIDA"}</td>
            <td>{r.ts_resolved ? new Date(r.ts_resolved * 1000).toLocaleString() : "—"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Glossary() {
  return (
    <details className="glossary card">
      <summary>¿Qué significan estos términos?</summary>
      <dl className="glossary-list">
        {GLOSSARY.map(([term, def]) => (
          <div key={term} className="glossary-item">
            <dt>{term}</dt>
            <dd>{def}</dd>
          </div>
        ))}
      </dl>
    </details>
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
      <PlainSummary halted={data.halted} haltReason={data.halt_reason} stats={data.stats} label="cripto" />

      <div className="card">
        <h2>Indicadores en vivo</h2>
        <p className="card-subtitle">Cómo está el mercado ahora mismo para cada símbolo que sigue el bot — no implica que vaya a operar.</p>
        {symbols.length > 0 ? (
          <div className="indicator-grid">
            {symbols.map((symbol) => (
              <IndicatorCard key={symbol} symbol={symbol} snapshot={indicatorsBySymbol[symbol]} />
            ))}
          </div>
        ) : (
          <p className="empty">
            Esperando el primer ciclo exitoso — las tarjetas aparecen solas apenas se guarde el primer snapshot.
          </p>
        )}
      </div>

      <StatsRow title="Performance — Cripto (trades cerrados)" stats={data.stats} showProfitFactor />

      <div className="card">
        <h2>Posiciones abiertas</h2>
        <p className="card-subtitle">Operaciones en modo papel que el bot ya "abrió" y todavía no llegaron a su target ni a su stop.</p>
        <CryptoOpenTable rows={data.crypto_open} />
      </div>

      <div className="card">
        <h2>Equity</h2>
        <p className="card-subtitle">Evolución del capital simulado a lo largo del tiempo.</p>
        {lastEquity !== null && <div className="equity-value">${lastEquity.toFixed(2)}</div>}
        <EquitySparkline points={data.equity} />
      </div>

      <div className="card">
        <h2>Bitácora de decisiones</h2>
        <p className="card-subtitle">Cada vez que el bot detecta una señal, queda registrado acá qué decidió hacer con ella.</p>
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
                  <td>{directionLabel(d.direction)}</td>
                  <td>{d.confidence ? "★".repeat(d.confidence) : "—"}</td>
                  <td className={d.risk_pass ? "ok" : "fail"}>{d.risk_pass ? "OK" : "Bloqueada"}</td>
                  <td className={`decision-${d.decision}`}>{decisionLabel(d.decision)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <Glossary />
    </>
  );
}

function PolymarketTab({ data }) {
  return (
    <>
      <PlainSummary halted={false} stats={data.polymarket_stats} label="Polymarket" />

      <StatsRow title="Performance — Polymarket (señales resueltas)" stats={data.polymarket_stats}
        emptyMessage="Sin señales resueltas todavía — las métricas aparecen cuando el motor encuentre y cierre alguna." />

      <div className="card">
        <h2>Performance por categoría</h2>
        <p className="card-subtitle">Mismo desempeño de arriba, pero desglosado por el tema del mercado (clima, política, cripto, etc.).</p>
        <PolymarketCategoryTable byCategory={data.polymarket_stats_by_category} />
      </div>

      <div className="card">
        <h2>Señales abiertas ({data.polymarket_open?.length || 0})</h2>
        <p className="card-subtitle">Mercados de predicción que el bot encontró y todavía no se resolvieron.</p>
        <PolymarketOpenTable rows={data.polymarket_open} />
      </div>

      <div className="card">
        <h2>Historial reciente</h2>
        <PolymarketResolvedTable rows={data.polymarket_resolved} />
      </div>

      <Glossary />
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
        <div>
          <h1>Trader IA 24/7</h1>
          <p className="header-subtitle">Panel de control del bot — operaciones en cripto y mercados de predicción</p>
        </div>
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
