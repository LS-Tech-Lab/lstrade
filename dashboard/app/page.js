"use client";
import { useEffect, useRef, useState } from "react";

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

// El bot guarda un snapshot por símbolo en cada ciclo — el cron externo
// (cron-job.org) hoy dispara /api/cycle cada ~10 minutos, así que un
// snapshot recién guardado siempre debería tener pocos minutos. Si algo
// se rompe en el medio (cron caído, símbolo con error, un método que
// falta como pasó con record_indicator_snapshot), el dato queda "clavado"
// sin ningún error visible — la única señal de que algo anda mal es que
// deja de cambiar. Esto lo hace explícito en vez de exigir que alguien
// note "che, esto no varía desde ayer".
const FRESHNESS_WARN_MINUTES = 20;   // 2x el intervalo esperado del cron
const FRESHNESS_FAIL_MINUTES = 60;   // 6x — casi seguro que el cron dejó de correr

function freshnessState(ts) {
  if (!ts) return { minutes: null, text: "Sin datos", tone: "" };
  const minutes = (Date.now() / 1000 - ts) / 60;
  if (minutes >= FRESHNESS_FAIL_MINUTES) {
    return { minutes, text: `Sin actualizar hace ${Math.round(minutes)} min — revisá el cron`, tone: "fail" };
  }
  if (minutes >= FRESHNESS_WARN_MINUTES) {
    return { minutes, text: `Desactualizado (${Math.round(minutes)} min)`, tone: "warn" };
  }
  return { minutes, text: minutes < 1 ? "Al día" : `Hace ${Math.round(minutes)} min`, tone: "ok" };
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

// Formatea un timestamp unix (segundos) en fecha corta, para el tooltip
// del gráfico de equity.
function formatChartDate(ts) {
  return new Date(ts * 1000).toLocaleString(undefined, {
    day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit",
  });
}

// Gráfico de equity — el elemento central del panel: es lo primero que
// alguien quiere ver ("¿cómo viene la plata?"), así que es el único lugar
// donde el dashboard se permite un poco de espectáculo (relleno con
// degradé, cuadrícula, tooltip al pasar el dedo/mouse). El resto del panel
// se mantiene deliberadamente tranquilo alrededor de esto.
function EquityChart({ points }) {
  const [hoverIdx, setHoverIdx] = useState(null);

  if (!points || points.length < 2) {
    return <div className="empty">Todavía no hay suficiente historial de equity.</div>;
  }

  const values = points.map((p) => p.equity);
  const min = Math.min(...values);
  const max = Math.min(...values) === Math.max(...values) ? min + 1 : Math.max(...values);
  const w = 640, h = 200, padX = 4, padTop = 14, padBottom = 24;
  const xAt = (i) => padX + (i / (points.length - 1)) * (w - padX * 2);
  const yAt = (v) => padTop + (1 - (v - min) / (max - min)) * (h - padTop - padBottom);

  const linePoints = points.map((p, i) => `${xAt(i)},${yAt(p.equity)}`).join(" ");
  const areaPoints = `${xAt(0)},${h - padBottom} ${linePoints} ${xAt(points.length - 1)},${h - padBottom}`;

  const first = values[0];
  const last = values[values.length - 1];
  const changePct = first !== 0 ? ((last - first) / Math.abs(first)) * 100 : 0;
  const positive = changePct >= 0;

  const hover = hoverIdx !== null ? points[hoverIdx] : null;

  function handleMove(clientX, svgEl) {
    const rect = svgEl.getBoundingClientRect();
    const relX = ((clientX - rect.left) / rect.width) * w;
    let nearest = 0, best = Infinity;
    points.forEach((p, i) => {
      const d = Math.abs(xAt(i) - relX);
      if (d < best) { best = d; nearest = i; }
    });
    setHoverIdx(nearest);
  }

  const gridLines = [0.25, 0.5, 0.75];

  return (
    <div className="equity-chart-wrap">
      <div className="equity-chart-header">
        <div className="equity-value">${last.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</div>
        <div className={`equity-change ${positive ? "ok" : "fail"}`}>
          {positive ? "▲" : "▼"} {Math.abs(changePct).toFixed(2)}% desde el inicio del historial
        </div>
      </div>
      <svg
        viewBox={`0 0 ${w} ${h}`}
        className="equity-chart"
        preserveAspectRatio="none"
        onMouseMove={(e) => handleMove(e.clientX, e.currentTarget)}
        onMouseLeave={() => setHoverIdx(null)}
        onTouchMove={(e) => { if (e.touches[0]) handleMove(e.touches[0].clientX, e.currentTarget); }}
        onTouchEnd={() => setHoverIdx(null)}
      >
        <defs>
          <linearGradient id="equityFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--accent)" stopOpacity="0.32" />
            <stop offset="100%" stopColor="var(--accent)" stopOpacity="0" />
          </linearGradient>
        </defs>

        {gridLines.map((g) => (
          <line key={g} x1={padX} x2={w - padX} y1={padTop + g * (h - padTop - padBottom)} y2={padTop + g * (h - padTop - padBottom)}
            className="equity-grid-line" />
        ))}

        <polygon points={areaPoints} fill="url(#equityFill)" className="equity-area" />
        <polyline points={linePoints} fill="none" stroke="var(--accent)" strokeWidth="2.25"
          strokeLinejoin="round" strokeLinecap="round" className="equity-line" />

        {hover && (
          <g className="equity-hover">
            <line x1={xAt(hoverIdx)} x2={xAt(hoverIdx)} y1={padTop} y2={h - padBottom} className="equity-crosshair" />
            <circle cx={xAt(hoverIdx)} cy={yAt(hover.equity)} r="4" className="equity-hover-dot" />
          </g>
        )}
        {!hover && (
          <circle cx={xAt(points.length - 1)} cy={yAt(last)} r="4" className="equity-hover-dot equity-hover-dot-static" />
        )}
      </svg>
      {hover && (
        <div
          className="equity-tooltip"
          style={{ left: `${(xAt(hoverIdx) / w) * 100}%` }}
        >
          <div className="equity-tooltip-value">${hover.equity.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</div>
          <div className="equity-tooltip-date">{formatChartDate(hover.ts)}</div>
        </div>
      )}
    </div>
  );
}

function StatCard({ label, value, suffix = "", tone, info, barPct }) {
  return (
    <div className={`stat-card ${tone || ""}`}>
      <div className="stat-label">
        {label}
        {info && <Info text={info} />}
      </div>
      <div className="stat-value">{value === null || value === undefined ? "—" : `${value}${suffix}`}</div>
      {barPct !== undefined && (
        <div className="stat-bar-track">
          <div className={`stat-bar-fill ${tone || ""}`} style={{ width: `${Math.max(0, Math.min(100, barPct))}%` }} />
        </div>
      )}
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
          info={GLOSSARY.find(([k]) => k === "Win rate")[1]} barPct={stats.win_rate} />
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
  const freshness = freshnessState(snapshot.ts);
  return (
    <div className={`indicator-card ${freshness.tone === "fail" ? "stale-fail" : freshness.tone === "warn" ? "stale-warn" : ""}`}>
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
      <div className="indicator-ts">
        <span className={`freshness-dot ${freshness.tone}`} />
        <span className={freshness.tone === "" ? "" : freshness.tone}>{freshness.text}</span>
        <span className="indicator-ts-sep">·</span>
        {new Date(snapshot.ts * 1000).toLocaleTimeString()}
      </div>
    </div>
  );
}

// NUEVO: envuelve cualquier tabla ancha para que en pantallas chicas se
// pueda desplazar horizontalmente en vez de desbordar el layout o achicar
// el texto hasta ser ilegible. El borde/degradé lateral es la señal visual
// de "hay más contenido para el costado".
function TableScroll({ children }) {
  return <div className="table-scroll">{children}</div>;
}

function CryptoOpenTable({ rows }) {
  if (!rows || rows.length === 0) {
    return <p className="empty">Sin posiciones cripto abiertas ahora mismo.</p>;
  }
  return (
    <TableScroll>
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
            <td data-label="Símbolo">{r.symbol}</td>
            <td data-label="Dirección">{directionLabel(r.direction)}</td>
            <td data-label="Entrada">{r.entry_price?.toFixed(6)}</td>
            <td data-label="Target">{r.target_price?.toFixed(6)}</td>
            <td data-label="Stop">{r.current_stop?.toFixed(6)}</td>
            <td data-label="Tamaño">{r.position_size?.toFixed(6)}</td>
            <td data-label="Abierta">{new Date(r.ts_opened * 1000).toLocaleString()}</td>
          </tr>
        ))}
      </tbody>
    </table>
    </TableScroll>
  );
}

function PolymarketCategoryTable({ byCategory }) {
  const rows = Object.entries(byCategory || {}).sort((a, b) => b[1].total_r - a[1].total_r);
  if (rows.length === 0) {
    return <p className="empty">Todavía no hay señales resueltas para desglosar por categoría.</p>;
  }
  const maxAbs = Math.max(...rows.map(([, s]) => Math.abs(s.total_r)), 0.01);
  return (
    <TableScroll>
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
          const barPct = (Math.abs(s.total_r) / maxAbs) * 100;
          return (
            <tr key={cat}>
              <td data-label="Categoría">{cat}{s.n < 5 ? " ⚠️" : ""}</td>
              <td data-label="n">{s.n}</td>
              <td data-label="Win%">{s.win_rate.toFixed(0)}%</td>
              <td data-label="Expectancy" className={tone}>{s.expectancy_r >= 0 ? "+" : ""}{s.expectancy_r.toFixed(2)}R</td>
              <td data-label="PF">{s.profit_factor !== null ? s.profit_factor.toFixed(2) : "—"}</td>
              <td data-label="Total R" className={tone}>
                <div className="cell-bar-wrap">
                  <span>{s.total_r >= 0 ? "+" : ""}{s.total_r.toFixed(2)}R</span>
                  <div className="cell-bar-track">
                    <div className={`cell-bar-fill ${tone}`} style={{ width: `${barPct}%` }} />
                  </div>
                </div>
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
    </TableScroll>
  );
}

function PolymarketOpenTable({ rows }) {
  if (!rows || rows.length === 0) {
    return <p className="empty">Sin señales de Polymarket abiertas ahora mismo.</p>;
  }
  return (
    <TableScroll>
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
            <td data-label="Mercado">{r.question?.length > 60 ? `${r.question.slice(0, 60)}…` : r.question}</td>
            <td data-label="Dirección">{directionLabel(r.direction)}</td>
            <td data-label="Entrada">{r.entry?.toFixed(3)}</td>
            <td data-label="Target">{r.target?.toFixed(3)}</td>
            <td data-label="Stop">{r.stop?.toFixed(3)}</td>
            <td data-label="Enviada">{new Date(r.ts_signaled * 1000).toLocaleString()}</td>
          </tr>
        ))}
      </tbody>
    </table>
    </TableScroll>
  );
}

function PolymarketResolvedTable({ rows }) {
  if (!rows || rows.length === 0) {
    return <p className="empty">Todavía no hay señales resueltas.</p>;
  }
  return (
    <TableScroll>
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
            <td data-label="Mercado">{r.question?.length > 60 ? `${r.question.slice(0, 60)}…` : r.question}</td>
            <td data-label="Dirección">{directionLabel(r.direction)}</td>
            <td data-label="Resultado" className={r.outcome === "target" ? "ok" : "fail"}>{r.outcome === "target" ? "GANADA" : "PERDIDA"}</td>
            <td data-label="Resuelta">{r.ts_resolved ? new Date(r.ts_resolved * 1000).toLocaleString() : "—"}</td>
          </tr>
        ))}
      </tbody>
    </table>
    </TableScroll>
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

// NUEVO: carrusel horizontal para las tarjetas de indicadores. Antes era
// una grilla que en mobile terminaba siendo una lista vertical larga —
// se vuelve incómodo apenas se agregan más símbolos en SYMBOLS (.env).
// Es scroll nativo con snap (sin librerías), con puntos abajo que
// muestran cuál tarjeta está a la vista y permiten saltar directo.
function IndicatorCarousel({ symbols, indicatorsBySymbol }) {
  const containerRef = useRef(null);
  const [active, setActive] = useState(0);

  function handleScroll() {
    const el = containerRef.current;
    if (!el || el.children.length === 0) return;
    const cardWidth = el.children[0].offsetWidth + 12; // + gap
    setActive(Math.round(el.scrollLeft / cardWidth));
  }

  function goTo(i) {
    const el = containerRef.current;
    if (!el || !el.children[i]) return;
    el.children[i].scrollIntoView({ behavior: "smooth", inline: "start", block: "nearest" });
  }

  return (
    <div className="indicator-carousel">
      <div className="indicator-carousel-track" ref={containerRef} onScroll={handleScroll}>
        {symbols.map((symbol) => (
          <IndicatorCard key={symbol} symbol={symbol} snapshot={indicatorsBySymbol[symbol]} />
        ))}
      </div>
      {symbols.length > 1 && (
        <div className="carousel-dots">
          {symbols.map((symbol, i) => (
            <button
              key={symbol}
              className={`carousel-dot ${i === active ? "active" : ""}`}
              aria-label={`Ir a ${symbol}`}
              onClick={() => goTo(i)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function CriptoTab({ data }) {
  const indicatorsBySymbol = Object.fromEntries((data.indicators || []).map((s) => [s.symbol, s]));
  // Símbolos a mostrar: unión de lo configurado (inferido de los snapshots
  // recibidos) y lo que aparece en la bitácora, así no queda un símbolo
  // fuera solo porque nunca tuvo señal de trading.
  const symbols = Array.from(
    new Set([...(data.indicators || []).map((s) => s.symbol), ...data.decisions.map((d) => d.symbol)])
  );
  const staleSymbols = symbols.filter((s) => {
    const f = freshnessState(indicatorsBySymbol[s]?.ts);
    return f.tone === "warn" || f.tone === "fail";
  });

  return (
    <>
      <PlainSummary halted={data.halted} haltReason={data.halt_reason} stats={data.stats} label="cripto" />

      <div className="card">
        <h2>Indicadores en vivo</h2>
        <p className="card-subtitle">Cómo está el mercado ahora mismo para cada símbolo que sigue el bot — no implica que vaya a operar.</p>
        {staleSymbols.length > 0 && (
          <div className="stale-banner">
            ⚠ {staleSymbols.join(", ")} no se {staleSymbols.length > 1 ? "actualizan" : "actualiza"} desde hace rato — puede que el cron externo no esté corriendo.
          </div>
        )}
        {symbols.length > 0 ? (
          <IndicatorCarousel symbols={symbols} indicatorsBySymbol={indicatorsBySymbol} />
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
        <EquityChart points={data.equity} />
      </div>

      <div className="card">
        <h2>Bitácora de decisiones</h2>
        <p className="card-subtitle">Cada vez que el bot detecta una señal, queda registrado acá qué decidió hacer con ella.</p>
        {data.decisions.length === 0 ? (
          <p className="empty">Todavía no hay decisiones registradas.</p>
        ) : (
          <TableScroll>
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
                  <td data-label="Fecha">{new Date(d.ts * 1000).toLocaleString()}</td>
                  <td data-label="Símbolo">{d.symbol}</td>
                  <td data-label="Señal">{d.signal_type || "—"}</td>
                  <td data-label="Dirección">{directionLabel(d.direction)}</td>
                  <td data-label="Confianza">{d.confidence ? "★".repeat(d.confidence) : "—"}</td>
                  <td data-label="Riesgo" className={d.risk_pass ? "ok" : "fail"}>{d.risk_pass ? "OK" : "Bloqueada"}</td>
                  <td data-label="Decisión" className={`decision-${d.decision}`}>{decisionLabel(d.decision)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          </TableScroll>
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
