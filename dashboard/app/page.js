"use client";
import { useEffect, useRef, useState } from "react";

// ────────────────────────────────────────────────────────────────────
// Diccionario en español simple. Centraliza las explicaciones de los
// términos técnicos (RSI, expectancy, profit factor, etc.) para que no
// queden regadas por todo el archivo, y para que alguien sin experiencia
// en trading pueda entender cada número sin tener que buscarlo aparte.
// ────────────────────────────────────────────────────────────────────
const GLOSSARY = [
  [ "Win rate", "De cada 100 operaciones cerradas, cuántas terminaron ganando." ],
  [ "Expectancy (R)", "Ganancia o pérdida promedio por operación, medida en 'múltiplos de riesgo' (R). +0.30R significa que, en promedio, cada operación gana un 30% de lo que se arriesgó en ella." ],
  [ "Profit factor", "Cuánto se ganó por cada $1 que se perdió. Un valor de 1.50 significa que por cada dólar perdido se ganaron $1.50. Por debajo de 1.0 el sistema pierde dinero en conjunto." ],
  [ "RSI", "Mide si un precio subió o bajó demasiado rápido en los últimos períodos. Arriba de 72 se considera 'sobrecomprado' (riesgo de corrección a la baja); debajo de 28, 'sobrevendido' (riesgo de rebote al alza)." ],
  [ "Momentum", "Cuánto cambió el precio en las últimas velas. Positivo = viene subiendo, negativo = viene bajando." ],
  [ "Volatilidad", "Qué tan bruscos son los movimientos de precio recientes. Más alto = movimientos más erráticos." ],
  [ "ATR", "Rango de movimiento típico de cada vela, en porcentaje del precio. Sirve como referencia de qué tan 'ancho' se mueve el mercado ahora mismo." ],
  [ "Vol. ratio", "Actividad de compra/venta comparada con lo normal. 1.0x = actividad normal, 2.0x = el doble de lo habitual." ],
  [ "Tendencia (bias)", "Hacia dónde apunta el precio en el mediano plazo, comparando dos promedios móviles. Alcista = viene subiendo, bajista = viene bajando." ],
  [ "Mi probabilidad", "Qué probabilidad le calcula el modelo del bot a que un bucket de temperatura ocurra, en base a los pronósticos (NWS, METAR, TAF) de la estación. Se compara contra el precio de mercado para ver si hay ventaja." ],
  [ "EV (valor esperado)", "La ventaja que ve el bot entre su probabilidad estimada y el precio de mercado, antes de tomar la señal. EV +15% significa que el bot cree que el mercado está subvalorando ese resultado en un 15%." ],
  [ "Retorno promedio (clima)", "Ganancia o pérdida promedio por señal de clima resuelta, si se hubiera apostado $1 a 'SI' al precio de mercado del momento. -100% significa perder toda la apuesta." ],
  [ "Brier score", "Qué tan calibrada estuvo la probabilidad del modelo contra lo que realmente pasó, en cada señal resuelta (clima o MLB). 0 = predicciones perfectas, 0.25 = tan bueno como tirar una moneda, 1 = siempre confiado y siempre equivocado." ],
  [ "Retorno promedio (MLB)", "Ganancia o pérdida promedio por señal de MLB resuelta, si se hubiera apostado $1 al lado (equipo) que eligió el modelo, al precio de mercado del momento. -100% significa que ese equipo perdió." ],
  [ "R-múltiple", "Cuántas veces el riesgo inicial se ganó o perdió. +1.5R significa que se ganó 1.5 veces lo que se arriesgó. -1R significa que se perdió todo el riesgo." ]
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
  if (dir === "YES") return "SÍ";
  if (dir === "NO") return "NO";
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
  const minutes = (Date.now() - parseTs(ts).getTime()) / 60000;
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
      ? <span className="info-tooltip">{text}</span>
    </span>
  );
}

// NUEVO: desde el commit "Change timestamps to ISO 8601 format in Supabase
// DB" (1 sep), supabase_db.py guarda los ts nuevos como string ISO 8601
// (timestamptz nativo de Postgres) en vez de segundos unix. Pero las filas
// más viejas en la base todavía tienen el formato anterior (número). Este
// helper soporta los dos: string → new Date() lo parsea directo; número →
// se asume que sigue siendo segundos unix, así que se multiplica por 1000.
// Todo el dashboard debe usar esto en vez de `new Date(x * 1000)` a mano.
function parseTs(ts) {
  if (ts === null || ts === undefined) return null;
  return typeof ts === "string" ? new Date(ts) : new Date(ts * 1000);
}

// Formatea un timestamp (ver parseTs) en fecha corta, para el tooltip
// del gráfico de equity.
function formatChartDate(ts) {
  return parseTs(ts).toLocaleString(undefined, {
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
          {positive ? "▲ " : "▼ "} {Math.abs(changePct).toFixed(2)}% desde el inicio del historial
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
    text = `De ${stats.n} operaciones de ${label} cerradas, el ${stats.win_rate.toFixed(0)}% fueron ganadoras.`
      + (stats.expectancy_r !== undefined && stats.expectancy_r !== null
        ? (positive
          ? " En promedio, el sistema está ganando por operación."
          : " En promedio, el sistema está perdiendo por operación — vale la pena revisar la estrategia.")
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
      <button className={`tab ${active === "clima" ? "active" : ""}`} onClick={() => onChange("clima")}>
        Clima
      </button>
      <button className={`tab ${active === "mlb" ? "active" : ""}`} onClick={() => onChange("mlb")}>
        MLB
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
        {parseTs(snapshot.ts).toLocaleTimeString()}
      </div>
    </div>
  );
}

// NOTA (05/09/2026): TableScroll (wrapper de scroll horizontal para
// <table>) se sacó de acá -- clima y MLB eran los últimos dos módulos que
// todavía usaban tablas HTML crudas; ahora los cuatro (cripto, Polymarket,
// clima, MLB) comparten el mismo carrusel de tarjetas (RowCarousel más
// abajo), que ya resuelve el mismo problema de forma más legible en mobile.

// NUEVO: hook compartido por todos los carruseles (indicadores, posiciones
// abiertas, bitácora de decisiones). Centraliza el cálculo de "qué tarjeta
// está a la vista" (para los puntos/flechas) y el scroll programático al
// hacer click en una flecha o un punto — antes esta lógica estaba duplicada
// solo para el carrusel de indicadores.
function useCarouselNav() {
  const containerRef = useRef(null);
  const [active, setActive] = useState(0);
  // FIX: antes esto corría sin throttle en CADA evento "scroll" — con
  // scroll-behavior: smooth (ver goTo) eso dispara decenas de re-renders
  // seguidos por un solo desplazamiento. Se agenda a lo sumo un cálculo por
  // frame con requestAnimationFrame; si ya hay uno pendiente, los eventos
  // intermedios se ignoran.
  const rafRef = useRef(null);
  function handleScroll() {
    if (rafRef.current !== null) return;
    rafRef.current = requestAnimationFrame(() => {
      
