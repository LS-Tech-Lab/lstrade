import { NextResponse } from "next/server";
import { createClient } from "@supabase/supabase-js";
import categorySpec from "../../../polymarket_categories.json";

// FIX: sin esto, Next.js puede pre-renderizar este route handler como
// estático (no usa cookies()/headers() ni ninguna otra API dinámica), y
// entonces sirve siempre la MISMA respuesta cacheada del build, sin volver
// a consultar Supabase — el `cache: "no-store"` del fetch en page.js solo
// evita el caché del navegador, no el caché de ejecución del handler en el
// servidor. Por eso el equity (y el resto del dashboard) se veía congelado
// aunque los datos en Supabase sí cambiaban.
export const dynamic = "force-dynamic";

function getClient() {
  return createClient(process.env.SUPABASE_URL, process.env.SUPABASE_SERVICE_KEY);
}

function computeStats(rows) {
  const valid = (rows || []).filter((r) => r.r_multiple !== null && r.r_multiple !== undefined);
  if (valid.length === 0) return { n: 0, win_rate: null, expectancy_r: null, profit_factor: null };
  const wins = valid.filter((r) => r.outcome === "target");
  const losses = valid.filter((r) => r.outcome === "stop");
  const grossWin = wins.reduce((s, r) => s + Math.max(0, r.r_multiple), 0);
  const grossLoss = Math.abs(losses.reduce((s, r) => s + Math.min(0, r.r_multiple), 0));
  return {
    n: valid.length,
    win_rate: (wins.length / valid.length) * 100,
    expectancy_r: valid.reduce((s, r) => s + r.r_multiple, 0) / valid.length,
    profit_factor: grossLoss > 0 ? grossWin / grossLoss : null,
  };
}

// Categorías por keywords — leídas de polymarket_categories.json (fuente
// única compartida con polymarket_categories.py). Antes esto era un
// array hardcodeado en paralelo al de Python, con el riesgo de que se
// desincronizaran si alguien tocaba solo un lado; ahora ambos leen el
// mismo JSON y cambiar una regla es un solo edit.
const CATEGORY_RULES = categorySpec.rules.map((r) => [r.name, new RegExp(r.pattern, "i")]);
const FALLBACK_CATEGORY = categorySpec.fallback;

function categorize(question) {
  if (!question) return FALLBACK_CATEGORY;
  for (const [name, pattern] of CATEGORY_RULES) {
    if (pattern.test(question)) return name;
  }
  return FALLBACK_CATEGORY;
}

// FIX: este default tiene que ser IDÉNTICO char por char al de
// config.POLYMARKET_EXCLUDED_CATEGORIES (Python) — no comparten código (uno
// corre en el motor de señales, este en el dashboard serverless). Hasta
// ahora se habían ido desincronizando cada vez que se agregaba una
// categoría nueva de un solo lado: este default se había quedado en solo 2
// categorías mientras config.py ya tenía 4 ("Otros / sin clasificar" y
// "Cripto — objetivo de precio" faltaban acá). Si POLYMARKET_EXCLUDED_CATEGORIES
// no está seteada como env var real en Vercel, cada lado cae en SU PROPIO
// default y el panel termina mostrando un indicador "sin categorías
// excluidas" que no coincide con lo que el motor de señales realmente
// excluye. Se usa para que el indicador principal (win rate/expectancy/PF
// de arriba) no quede arrastrado por categorías ya identificadas como
// perdedoras — el historial completo sigue disponible sin filtrar en
// polymarket_resolved y en la tabla por categoría, esto solo afecta el
// resumen agregado.
//
// Mientras no compartan una sola fuente (ver el mismo comentario en
// config.py), cualquier cambio a esta lista se tiene que reflejar A MANO
// en AMBOS archivos.
const EXCLUDED_CATEGORIES = (
  process.env.POLYMARKET_EXCLUDED_CATEGORIES ||
  "Política / geopolítica,Redes sociales / figuras públicas,Otros / sin clasificar,Cripto — objetivo de precio"
).split(",").map((c) => c.trim()).filter(Boolean);

function computePolymarketStats(rows) {
  const resolved = (rows || []).filter((r) => r.outcome);
  if (resolved.length === 0) return { n: 0, win_rate: null };
  const wins = resolved.filter((r) => r.outcome === "target");
  return { n: resolved.length, win_rate: (wins.length / resolved.length) * 100 };
}

function computePolymarketStatsByCategory(resolvedSignals) {
  const byCategory = {};
  for (const r of resolvedSignals) {
    const stopDistance = Math.abs(r.entry - r.stop);
    if (stopDistance <= 0) continue;
    const rm = (r.exit_price - r.entry) / stopDistance;
    const cat = categorize(r.question);
    if (!byCategory[cat]) byCategory[cat] = [];
    byCategory[cat].push({ rm, outcome: r.outcome });
  }
  const result = {};
  for (const [cat, entries] of Object.entries(byCategory)) {
    const n = entries.length;
    const wins = entries.filter((e) => e.outcome === "target").length;
    const grossWin = entries.reduce((s, e) => s + Math.max(0, e.rm), 0);
    const grossLoss = Math.abs(entries.reduce((s, e) => s + Math.min(0, e.rm), 0));
    result[cat] = {
      n,
      win_rate: (wins / n) * 100,
      expectancy_r: entries.reduce((s, e) => s + e.rm, 0) / n,
      profit_factor: grossLoss > 0 ? grossWin / grossLoss : null,
      total_r: entries.reduce((s, e) => s + e.rm, 0),
    };
  }
  return result;
}

// NUEVO: stats de Clima. A diferencia de Polymarket genérico, acá no hay
// entry/target/stop — weather_signal_engine.py siempre evalúa comprar el
// lado YES de un bucket de temperatura (ver el comentario largo en
// api/weather_track_results.py), así que el retorno se simula como una
// apuesta de $1 nocional a YES al precio de mercado del momento de la
// señal: si resuelve 'yes' se cobra $1 (ganancia = 1/precio - 1), si
// resuelve 'no' se pierde el 100% de lo apostado.
// Se suma el Brier score porque es la métrica estándar para medir qué tan
// calibrada está una probabilidad estimada contra el resultado real — la
// tabla weather_signals se diseñó justo para esto (ver el comentario en
// schema.sql), pero hasta ahora nada lo calculaba.
function weatherReturnPct(row) {
  if (!row.outcome || !row.market_price || row.market_price <= 0) return null;
  return row.outcome === "yes" ? ((1 - row.market_price) / row.market_price) * 100 : -100;
}

function computeWeatherStats(resolvedSignals) {
  const valid = (resolvedSignals || []).filter((r) => r.outcome && r.market_price > 0);
  if (valid.length === 0) return { n: 0, win_rate: null, avg_return_pct: null, brier_score: null };
  const wins = valid.filter((r) => r.outcome === "yes");
  const returns = valid.map(weatherReturnPct).filter((r) => r !== null);
  const brierTerms = valid
    .filter((r) => r.my_prob !== null && r.my_prob !== undefined)
    .map((r) => Math.pow(r.my_prob - (r.outcome === "yes" ? 1 : 0), 2));
  return {
    n: valid.length,
    win_rate: (wins.length / valid.length) * 100,
    avg_return_pct: returns.length > 0 ? returns.reduce((s, r) => s + r, 0) / returns.length : null,
    brier_score: brierTerms.length > 0 ? brierTerms.reduce((s, b) => s + b, 0) / brierTerms.length : null,
  };
}

// NUEVO: stats de MLB. A diferencia de Clima (que siempre compra "SI" del
// bucket), mlb_signal_engine.py puede tomar YES o NO según de qué lado
// esté el EV -- por eso `outcome` acá NO es "yes"/"no" del mercado, es
// "win"/"loss" del LADO QUE SE COMPRÓ (ver resolve_mlb_signal en
// supabase_db.py). `market_price` ya es el precio pagado por ese lado
// puntual, así que la fórmula de retorno (1/precio - 1 si ganó, -100% si
// perdió) queda igual que en Clima sin necesidad de saber qué equipo era.
function mlbReturnPct(row) {
  if (!row.outcome || !row.market_price || row.market_price <= 0) return null;
  return row.outcome === "win" ? ((1 - row.market_price) / row.market_price) * 100 : -100;
}

function computeMlbStats(resolvedSignals) {
  const valid = (resolvedSignals || []).filter((r) => r.outcome && r.market_price > 0);
  if (valid.length === 0) return { n: 0, win_rate: null, avg_return_pct: null, brier_score: null };
  const wins = valid.filter((r) => r.outcome === "win");
  const returns = valid.map(mlbReturnPct).filter((r) => r !== null);
  const brierTerms = valid
    .filter((r) => r.my_prob !== null && r.my_prob !== undefined)
    .map((r) => Math.pow(r.my_prob - (r.outcome === "win" ? 1 : 0), 2));
  return {
    n: valid.length,
    win_rate: (wins.length / valid.length) * 100,
    avg_return_pct: returns.length > 0 ? returns.reduce((s, r) => s + r, 0) / returns.length : null,
    brier_score: brierTerms.length > 0 ? brierTerms.reduce((s, b) => s + b, 0) / brierTerms.length : null,
  };
}

// FIX: las listas de filas "abiertas" (sin resolver todavía) no tenían
// .limit() — en operación normal son chicas (unas pocas posiciones/señales
// esperando resolución), pero si el proceso que las resuelve se traba (cron
// caído, bug en *_track_results.py) crecen sin techo y cada carga del panel
// (cada 15s) traería la tabla entera. 100 es generoso para lo que realmente
// se muestra (el carrusel no pagina más allá de eso de forma usable).
const OPEN_ROWS_LIMIT = 100;
// FIX: antes pedía 200 filas de indicator_snapshots solo para quedarse con
// la más reciente POR SÍMBOLO (ver latestIndicatorsBySymbol abajo) — con 2-3
// símbolos y un snapshot cada ~10 min, 200 filas son ~33h de historial
// descartado en el cliente. 50 sigue dando varias horas de margen sin traer
// de más.
const INDICATOR_SNAPSHOT_LIMIT = 50;

export async function GET() {
  try {
    const supabase = getClient();

    const [
      equityRes,
      decisionsRes,
      stateRes,
      pendingRes,
      openTradesRes,
      closedTradesRes,
      polymarketOpenRes,
      polymarketResolvedRes,
      indicatorsRes,
      weatherOpenRes,
      weatherResolvedRes,
      mlbOpenRes,
      mlbResolvedRes,
    ] = await Promise.all([
      // FIX: antes traía las 200 filas MÁS VIEJAS (ascending + limit sin
      // order by desc primero) — con 600+ filas acumuladas, esa ventana
      // nunca llegaba a los datos recientes y el gráfico de equity se veía
      // eternamente clavado en el valor inicial. Se pide descendente (las
      // últimas 200) y se revierte abajo para mantener el orden cronológico
      // ascendente que espera el frontend.
      supabase.from("equity_history").select("ts,equity").order("ts", { ascending: false }).limit(200),
      supabase.from("decisions").select("*").order("ts", { ascending: false }).limit(30),
      supabase.from("bot_state").select("*"),
      supabase.from("pending_decisions").select("*").eq("resolved", false),
      // NUEVO: posiciones cripto abiertas (modo papel) — antes run_cycle()
      // nunca las registraba, así que esta tabla estaba siempre vacía.
      supabase.from("open_trades").select("*").order("ts_opened", { ascending: false }).limit(OPEN_ROWS_LIMIT),
      supabase.from("closed_trades").select("outcome,r_multiple").order("ts_closed", { ascending: false }).limit(500),
      // Señales de Polymarket todavía sin resolver — "posiciones abiertas" de ese módulo.
      supabase.from("polymarket_signals").select("*").is("outcome", null).order("ts_signaled", { ascending: false }).limit(OPEN_ROWS_LIMIT),
      // Últimas resueltas: para el historial reciente y las stats por categoría.
      supabase.from("polymarket_signals").select("*").not("outcome", "is", null).order("ts_resolved", { ascending: false }).limit(200),
      // NUEVO: último snapshot de indicadores por símbolo (ver
      // indicator_snapshots en schema.sql) — antes el dashboard solo podía
      // mostrar RSI/tendencia en los raros ciclos donde hubo señal real.
      supabase.from("indicator_snapshots").select("*").order("ts", { ascending: false }).limit(INDICATOR_SNAPSHOT_LIMIT),
      // NUEVO: señales de Clima — corrían y se guardaban en weather_signals
      // desde hace rato, pero el dashboard nunca las consultaba (a
      // diferencia de Cripto y Polymarket, Clima no tenía ningún tab).
      supabase.from("weather_signals").select("*").is("outcome", null).order("ts_signaled", { ascending: false }).limit(OPEN_ROWS_LIMIT),
      supabase.from("weather_signals").select("*").not("outcome", "is", null).order("ts_resolved", { ascending: false }).limit(200),
      // NUEVO: señales de MLB (ver mlb_signal_engine.py / run_mlb_cycle en app.py).
      supabase.from("mlb_signals").select("*").is("outcome", null).order("ts_signaled", { ascending: false }).limit(OPEN_ROWS_LIMIT),
      supabase.from("mlb_signals").select("*").not("outcome", "is", null).order("ts_resolved", { ascending: false }).limit(200),
    ]);

    // FIX: antes un fallo puntual en cualquiera de estas 4 (equity_history,
    // decisions, bot_state, pending_decisions) devolvía 500 y tumbaba TODO
    // el panel — incluso sin relación entre sí (equity y pending, por
    // ejemplo, son completamente independientes entre sí y del resto). Ahora
    // se tratan igual que las otras 7 secciones: se listan en
    // failed_sections y el resto del panel sigue funcionando con lo que sí
    // cargó, en vez de una pantalla de error total por un problema parcial.
    const namedResults = {
      equity: equityRes,
      decisions: decisionsRes,
      bot_state: stateRes,
      pending: pendingRes,
      crypto_open: openTradesRes,
      crypto_stats: closedTradesRes,
      polymarket_open: polymarketOpenRes,
      polymarket_resolved: polymarketResolvedRes,
      indicators: indicatorsRes,
      weather_open: weatherOpenRes,
      weather_resolved: weatherResolvedRes,
      mlb_open: mlbOpenRes,
      mlb_resolved: mlbResolvedRes,
    };
    const failedSections = Object.entries(namedResults)
      .filter(([, res]) => res.error)
      .map(([name, res]) => {
        console.error(`[api/data] fallo cargando "${name}":`, res.error.message);
        return name;
      });

    const stateMap = Object.fromEntries((stateRes.data || []).map((r) => [r.key, r.value]));

    // Un snapshot por símbolo (el más reciente) — la tabla puede tener
    // varias filas históricas por símbolo, acá solo interesa la última.
    const latestIndicatorsBySymbol = {};
    for (const row of indicatorsRes.data || []) {
      if (!latestIndicatorsBySymbol[row.symbol]) latestIndicatorsBySymbol[row.symbol] = row;
    }

    const resolvedSignals = polymarketResolvedRes.error ? [] : (polymarketResolvedRes.data || []);
    const weatherResolved = weatherResolvedRes.error ? [] : (weatherResolvedRes.data || []);
    const mlbResolved = mlbResolvedRes.error ? [] : (mlbResolvedRes.data || []);
    // "Core" = sin las categorías excluidas (ver EXCLUDED_CATEGORIES) — es
    // lo que se muestra como indicador principal para que una categoría ya
    // identificada como mala no tape el desempeño real del resto.
    const resolvedSignalsCore = resolvedSignals.filter((r) => !EXCLUDED_CATEGORIES.includes(categorize(r.question)));

    return NextResponse.json({
      equity: (equityRes.data || []).slice().reverse(),
      decisions: decisionsRes.data || [],
      halted: stateMap.trading_halted === "1",
      halt_reason: stateMap.halt_reason || null,
      pending: pendingRes.data || [],
      // Nombres de las secciones que fallaron al cargar (ver namedResults
      // arriba) — vacío no dice "no hay datos", significa "sí cargó y
      // está vacío". El frontend usa esto para avisar cuáles secciones
      // están mostrando datos viejos/incompletos en vez de "sin datos".
      failed_sections: failedSections,
      // Si las tablas todavía no existen (schema.sql viejo sin correr de
      // nuevo), no rompemos el dashboard — se muestran vacías.
      crypto_open: openTradesRes.error ? [] : (openTradesRes.data || []),
      stats: closedTradesRes.error ? { n: 0, win_rate: null, expectancy_r: null, profit_factor: null }
        : computeStats(closedTradesRes.data),
      polymarket_stats: polymarketResolvedRes.error ? { n: 0, win_rate: null }
        : computePolymarketStats(resolvedSignalsCore),
      polymarket_stats_all_categories: polymarketResolvedRes.error ? { n: 0, win_rate: null }
        : computePolymarketStats(resolvedSignals),
      polymarket_excluded_categories: EXCLUDED_CATEGORIES,
      polymarket_stats_by_category: computePolymarketStatsByCategory(resolvedSignals),
      polymarket_open: polymarketOpenRes.error ? [] : (polymarketOpenRes.data || []),
      polymarket_resolved: resolvedSignals.slice(0, 20),
      indicators: indicatorsRes.error ? [] : Object.values(latestIndicatorsBySymbol),
      weather_open: weatherOpenRes.error ? [] : (weatherOpenRes.data || []),
      weather_resolved: weatherResolved.slice(0, 20),
      weather_stats: weatherResolvedRes.error ? { n: 0, win_rate: null, avg_return_pct: null, brier_score: null }
        : computeWeatherStats(weatherResolved),
      mlb_open: mlbOpenRes.error ? [] : (mlbOpenRes.data || []),
      mlb_resolved: mlbResolved.slice(0, 20),
      mlb_stats: mlbResolvedRes.error ? { n: 0, win_rate: null, avg_return_pct: null, brier_score: null }
        : computeMlbStats(mlbResolved),
    });
  } catch (e) {
    return NextResponse.json({ error: String(e) }, { status: 500 });
  }
          }
