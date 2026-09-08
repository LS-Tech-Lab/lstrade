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

// FIX (08/09/2026, auditoría pedida por LS): win_rate y profit_factor
// definían ganador/perdedor por el MOTIVO de cierre (outcome === "target"
// vs "stop") en vez del resultado real de la operación (signo de
// r_multiple). Eso subestimaba los dos números: un trade que sale por
// trailing stop pero cierra en verde (outcome="stop" con r_multiple>0 --
// pasa cuando el stop se mueve a favor tras avanzar el precio, 10 de los
// 58 trades al 08/09) contaba como derrota en win_rate, y no sumaba ni
// como ganancia ni como pérdida en profit_factor (quedaba afuera de
// `wins` por el filtro de outcome, y Math.min(0,r) lo neutralizaba del
// lado de `losses`). Con los 58 trades cerrados al 08/09 esto mostraba
// 34.5% / 1.71 en vez de los 51.7% / 1.93 reales.
// Ahora win/loss se define por el signo de r_multiple (estándar de la
// industria) y se agrega `breakdown` con el detalle por motivo de cierre
// (target / stop completo / breakeven / trailing parcial en verde) para
// que el tooltip del dashboard pueda explicar la composición sin perder
// esa información.
function computeStats(rows) {
  const valid = (rows || []).filter((r) => r.r_multiple !== null && r.r_multiple !== undefined);
  if (valid.length === 0) return { n: 0, win_rate: null, expectancy_r: null, profit_factor: null, breakdown: null };
  const wins = valid.filter((r) => r.r_multiple > 0);
  const losses = valid.filter((r) => r.r_multiple < 0);
  const grossWin = wins.reduce((s, r) => s + r.r_multiple, 0);
  const grossLoss = Math.abs(losses.reduce((s, r) => s + r.r_multiple, 0));

  // Desglose por motivo de cierre — puramente informativo, no altera
  // ninguna de las métricas de arriba.
  const targetHits = valid.filter((r) => r.outcome === "target");
  const fullStops = valid.filter((r) => r.outcome === "stop" && r.r_multiple < -0.5);
  const breakeven = valid.filter((r) => r.outcome === "stop" && Math.abs(r.r_multiple) < 0.01);
  const trailingPartial = valid.filter((r) => r.outcome === "stop" && r.r_multiple > 0.01);
  const sumR = (rows2) => rows2.reduce((s, r) => s + r.r_multiple, 0);

  return {
    n: valid.length,
    win_rate: (wins.length / valid.length) * 100,
    expectancy_r: valid.reduce((s, r) => s + r.r_multiple, 0) / valid.length,
    profit_factor: grossLoss > 0 ? grossWin / grossLoss : null,
    breakdown: {
      target: { n: targetHits.length, sum_r: sumR(targetHits) },
      full_stop: { n: fullStops.length, sum_r: sumR(fullStops) },
      breakeven: { n: breakeven.length, sum_r: sumR(breakeven) },
      trailing_partial: { n: trailingPartial.length, sum_r: sumR(trailingPartial) },
    },
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

// Se usa para que el indicador principal (win rate/expectancy/PF de
// arriba) no quede arrastrado por categorías ya identificadas como
// perdedoras — el historial completo sigue disponible sin filtrar en
// polymarket_resolved y en la tabla por categoría, esto solo afecta el
// resumen agregado.
//
// FIX (06/09/2026): default leído de categorySpec.excluded (mismo JSON
// que ya es fuente única de las reglas de categorización) en vez de un
// string hardcodeado acá aparte del que tiene config.py (Python) -- ya
// se habían desincronizado una vez (este default se había quedado sin
// "Clima", agregada del lado de Python el 04/09/2026 y nunca replicada
// acá). Se sigue permitiendo override real por env var
// POLYMARKET_EXCLUDED_CATEGORIES si hace falta cambiarla sin deploy.
const EXCLUDED_CATEGORIES = (
  process.env.POLYMARKET_EXCLUDED_CATEGORIES ||
  categorySpec.excluded.join(",")
).split(",").map((c) => c.trim()).filter(Boolean);

// FIX (06/09/2026): antes esta función solo devolvía {n, win_rate} -- por
// eso el card de arriba de Polymarket mostraba solo 2 métricas contra las
// 4 de Clima/MLB (ver captura del 06/09), aunque más abajo el desglose por
// categoría (computePolymarketStatsByCategory) sí calculaba R-múltiplo
// completo para las mismas señales. Se trae la misma fórmula acá (y la
// misma que ya usa el frontend en PolymarketResolvedTable para
// rMultiple/returnPct por fila) para que el agregado de arriba y el
// detalle de abajo siempre coincidan.
function computePolymarketStats(rows) {
  const resolved = (rows || []).filter((r) => r.outcome && r.exit_price !== null && r.exit_price !== undefined);
  if (resolved.length === 0) return { n: 0, win_rate: null, avg_return_pct: null, expectancy_r: null, profit_factor: null };
  const wins = resolved.filter((r) => r.outcome === "target");
  const rMultiples = resolved
    .map((r) => {
      const stopDistance = Math.abs(r.entry - r.stop);
      // FIX (06/09/2026): sin el * direction -- entry/target/stop siempre
      // vienen en el marco de precio del token que el bot efectivamente
      // compró (target > entry > stop sin importar si direction es YES o
      // NO; confirmado contra datos reales: 464/466 señales cumplen esto).
      // El multiplicador por direction invertía el signo de TODAS las
      // señales NO (228 de 466), convirtiendo wins reales en R negativo y
      // viceversa -- por eso el expectancy agregado daba +0.02R con datos
      // reales que en realidad rendían +0.12R (ver historial de esta
      // conversación, verificado contra polymarket_signals en Supabase).
      if (stopDistance <= 0) return null;
      return (r.exit_price - r.entry) / stopDistance;
    })
    .filter((rm) => rm !== null);
  const returns = resolved.filter((r) => r.entry > 0).map((r) => ((r.exit_price - r.entry) / r.entry) * 100);
  const grossWin = rMultiples.reduce((s, rm) => s + Math.max(0, rm), 0);
  const grossLoss = Math.abs(rMultiples.reduce((s, rm) => s + Math.min(0, rm), 0));
  return {
    n: resolved.length,
    win_rate: (wins.length / resolved.length) * 100,
    avg_return_pct: returns.length > 0 ? returns.reduce((s, r) => s + r, 0) / returns.length : null,
    expectancy_r: rMultiples.length > 0 ? rMultiples.reduce((s, rm) => s + rm, 0) / rMultiples.length : null,
    profit_factor: grossLoss > 0 ? grossWin / grossLoss : null,
  };
}

// NUEVO (08/09/2026, a pedido del usuario tras la calibración de Clima):
// ni Cripto ni Polymarket genérico calculan una probabilidad (my_prob) --
// usan `confidence` (entero 1-5) y `score`, y resuelven por r_multiple o
// target/stop, no por yes/no de mercado. No hay forma de armar una
// calibración de probabilidad literal con esos datos. Lo que sí se puede
// preguntar con lo que hay es la versión equivalente en espíritu: "¿una
// confianza más alta predice de verdad mejor resultado?" -- exactamente
// lo mismo que ya hacía computePolymarketStatsByCategory() de acá abajo,
// pero agrupando por `confidence` en vez de por categoría. Mismo shape de
// resultado ({n, win_rate, expectancy_r, profit_factor, total_r}) para
// poder reusar el mismo componente de tabla en el dashboard.
function computeStatsByConfidence(rows) {
  const byConfidence = {};
  for (const r of rows) {
    if (r.r_multiple === null || r.r_multiple === undefined) continue;
    const key = r.confidence !== null && r.confidence !== undefined ? `Confianza ${r.confidence}` : "Sin dato";
    if (!byConfidence[key]) byConfidence[key] = [];
    byConfidence[key].push(r.r_multiple);
  }
  const result = {};
  for (const [key, rms] of Object.entries(byConfidence)) {
    const n = rms.length;
    const wins = rms.filter((rm) => rm > 0).length;
    const grossWin = rms.reduce((s, rm) => s + Math.max(0, rm), 0);
    const grossLoss = Math.abs(rms.reduce((s, rm) => s + Math.min(0, rm), 0));
    result[key] = {
      n,
      win_rate: (wins / n) * 100,
      expectancy_r: rms.reduce((s, rm) => s + rm, 0) / n,
      profit_factor: grossLoss > 0 ? grossWin / grossLoss : null,
      total_r: rms.reduce((s, rm) => s + rm, 0),
    };
  }
  return result;
}

// Mismo cálculo que computeStatsByConfidence() pero para Polymarket
// genérico -- entry/target/stop/exit_price en vez de r_multiple directo
// (Polymarket no lo guarda como columna, se deriva igual que en
// computePolymarketStatsByCategory()/polymarket_stats_summary()).
function computePolymarketStatsByConfidence(resolvedSignals) {
  const byConfidence = {};
  for (const r of resolvedSignals) {
    const stopDistance = Math.abs(r.entry - r.stop);
    if (stopDistance <= 0) continue;
    const rm = (r.exit_price - r.entry) / stopDistance;
    const key = r.confidence !== null && r.confidence !== undefined ? `Confianza ${r.confidence}` : "Sin dato";
    if (!byConfidence[key]) byConfidence[key] = [];
    byConfidence[key].push({ rm, outcome: r.outcome });
  }
  const result = {};
  for (const [key, entries] of Object.entries(byConfidence)) {
    const n = entries.length;
    const wins = entries.filter((e) => e.outcome === "target").length;
    const grossWin = entries.reduce((s, e) => s + Math.max(0, e.rm), 0);
    const grossLoss = Math.abs(entries.reduce((s, e) => s + Math.min(0, e.rm), 0));
    result[key] = {
      n,
      win_rate: (wins / n) * 100,
      expectancy_r: entries.reduce((s, e) => s + e.rm, 0) / n,
      profit_factor: grossLoss > 0 ? grossWin / grossLoss : null,
      total_r: entries.reduce((s, e) => s + e.rm, 0),
    };
  }
  return result;
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
// NUEVO (06/09/2026): outcome='stop' -- salida anticipada por stop-loss
// (ver WEATHER_MLB_STOP_LOSS_PCT en config.py y run_weather_track_results
// en app.py). Antes una señal perdedora siempre resolvía -100% del
// nocional sin importar el precio pagado; ahora, si el precio cayó el
// umbral de stop ANTES de que el evento resolviera del todo, se cierra ahí
// y el retorno usa el precio real de salida (exit_price), no -100 fijo.
function weatherReturnPct(row) {
  if (!row.outcome || !row.market_price || row.market_price <= 0) return null;
  if (row.outcome === "yes") return ((1 - row.market_price) / row.market_price) * 100;
  if (row.outcome === "stop") {
    if (row.exit_price === null || row.exit_price === undefined) return -100;
    return ((row.exit_price - row.market_price) / row.market_price) * 100;
  }
  return -100; // "no"
}

function computeWeatherStats(resolvedSignals) {
  const valid = (resolvedSignals || []).filter((r) => r.outcome && r.market_price > 0);
  if (valid.length === 0) return { n: 0, win_rate: null, avg_return_pct: null, brier_score: null };
  const wins = valid.filter((r) => r.outcome === "yes");
  const returns = valid.map(weatherReturnPct).filter((r) => r !== null);
  // outcome='stop' es una salida ANTES de saber el resultado real del
  // evento -- no hay ground truth binario que comparar contra my_prob,
  // así que se excluye del Brier score (sí cuenta para n/win_rate/retorno).
  const brierTerms = valid
    .filter((r) => r.my_prob !== null && r.my_prob !== undefined && r.outcome !== "stop")
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
// NUEVO (06/09/2026): mismo agregado de outcome='stop' que weatherReturnPct
// -- ver comentario ahí arriba.
function mlbReturnPct(row) {
  if (!row.outcome || !row.market_price || row.market_price <= 0) return null;
  if (row.outcome === "win") return ((1 - row.market_price) / row.market_price) * 100;
  if (row.outcome === "stop") {
    if (row.exit_price === null || row.exit_price === undefined) return -100;
    return ((row.exit_price - row.market_price) / row.market_price) * 100;
  }
  return -100; // "loss"
}

function computeMlbStats(resolvedSignals) {
  // FIX (07/09/2026): se agregó el outcome "void" (partido cancelado sin
  // resultado jugado, ver run_mlb_track_results en app.py) después de que
  // este archivo ya existía -- acá no se lo excluía, así que un partido
  // cancelado se contaba como derrota tanto en win_rate como en Brier
  // (mlbReturnPct también le daba -100% por el fallback genérico). Un
  // partido que nunca se jugó no le suma ni le resta nada al modelo.
  const valid = (resolvedSignals || []).filter((r) => r.outcome && r.outcome !== "void" && r.market_price > 0);
  if (valid.length === 0) return { n: 0, win_rate: null, avg_return_pct: null, brier_score: null };
  const wins = valid.filter((r) => r.outcome === "win");
  const returns = valid.map(mlbReturnPct).filter((r) => r !== null);
  const brierTerms = valid
    .filter((r) => r.my_prob !== null && r.my_prob !== undefined && r.outcome !== "stop")
    .map((r) => Math.pow(r.my_prob - (r.outcome === "win" ? 1 : 0), 2));
  return {
    n: valid.length,
    win_rate: (wins.length / valid.length) * 100,
    avg_return_pct: returns.length > 0 ? returns.reduce((s, r) => s + r, 0) / returns.length : null,
    brier_score: brierTerms.length > 0 ? brierTerms.reduce((s, b) => s + b, 0) / brierTerms.length : null,
  };
}

// NUEVO (07/09/2026): calibración de MLB -- responde "cuando el modelo dice
// que un lado tiene 65% de ganar, ¿de verdad gana cerca del 65% de las
// veces?". Mismo criterio que weather_calibration_summary() en
// supabase_db.py (que existe hace rato pero nunca se conectó a ningún
// endpoint ni al dashboard -- quedó sin forma de verse). Se excluyen "stop"
// y "void": un stop-loss corta la posición antes de que el partido termine,
// así que no sabemos si ese lado realmente hubiera ganado o perdido; un
// void es un partido que no se jugó.
function computeMlbCalibration(resolvedSignals, bucketSize = 0.1) {
  const rows = (resolvedSignals || []).filter(
    (r) => (r.outcome === "win" || r.outcome === "loss") && r.my_prob !== null && r.my_prob !== undefined
  );
  if (rows.length === 0) return { n: 0, buckets: [] };
  const buckets = {};
  for (const r of rows) {
    const actual = r.outcome === "win" ? 1 : 0;
    // FIX (07/09/2026): +1e-9 antes del floor -- sin esto, 0.30/0.1 da
    // 2.9999999999999996 en JS (error de punto flotante normal, no un bug
    // de lógica) y una probabilidad de exactamente 30% caía en el bucket
    // "20-30%" en vez de "30-40%".
    const key = Math.min(Math.floor(r.my_prob / bucketSize + 1e-9), Math.floor(1 / bucketSize) - 1);
    if (!buckets[key]) buckets[key] = { predicted: [], actual: [] };
    buckets[key].predicted.push(r.my_prob);
    buckets[key].actual.push(actual);
  }
  const bucketRows = Object.keys(buckets)
    .map(Number)
    .sort((a, b) => a - b)
    .map((k) => {
      const b = buckets[k];
      return {
        range: `${(k * bucketSize * 100).toFixed(0)}-${((k + 1) * bucketSize * 100).toFixed(0)}%`,
        n: b.predicted.length,
        avg_predicted: b.predicted.reduce((s, x) => s + x, 0) / b.predicted.length,
        actual_freq: b.actual.reduce((s, x) => s + x, 0) / b.actual.length,
      };
    });
  return { n: rows.length, buckets: bucketRows };
}

// NUEVO (08/09/2026): equivalente de computeMlbCalibration() para Clima --
// mismo cálculo que ya existía en supabase_db.py (weather_calibration_summary)
// pero nunca se había conectado a ningún endpoint ni al dashboard. Acá el
// outcome resuelto es "yes"/"no" (no "win"/"loss" como MLB) y no hay
// concepto de "void" -- un "stop" sí se excluye, por el mismo motivo que en
// MLB: cortar la posición antes de que el mercado cierre no dice si el
// bucket elegido hubiera resuelto "yes" o "no" en la realidad.
function computeWeatherCalibration(resolvedSignals, bucketSize = 0.1) {
  const rows = (resolvedSignals || []).filter(
    (r) => (r.outcome === "yes" || r.outcome === "no") && r.my_prob !== null && r.my_prob !== undefined
  );
  if (rows.length === 0) return { n: 0, buckets: [] };
  const buckets = {};
  for (const r of rows) {
    const actual = r.outcome === "yes" ? 1 : 0;
    const key = Math.min(Math.floor(r.my_prob / bucketSize + 1e-9), Math.floor(1 / bucketSize) - 1);
    if (!buckets[key]) buckets[key] = { predicted: [], actual: [] };
    buckets[key].predicted.push(r.my_prob);
    buckets[key].actual.push(actual);
  }
  const bucketRows = Object.keys(buckets)
    .map(Number)
    .sort((a, b) => a - b)
    .map((k) => {
      const b = buckets[k];
      return {
        range: `${(k * bucketSize * 100).toFixed(0)}-${((k + 1) * bucketSize * 100).toFixed(0)}%`,
        n: b.predicted.length,
        avg_predicted: b.predicted.reduce((s, x) => s + x, 0) / b.predicted.length,
        actual_freq: b.actual.reduce((s, x) => s + x, 0) / b.actual.length,
      };
    });
  return { n: rows.length, buckets: bucketRows };
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
      equityWeatherRes,
      equityPolymarketRes,
      equityMlbRes,
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
      // AUDITORÍA (07/09/2026, pedido del usuario): se agrega el filtro
      // module="crypto" -- equity_history ahora guarda una serie por
      // módulo (ver migración add_module_to_equity_history en schema.sql),
      // así que sin este filtro esta consulta mezclaría puntos de las 4
      // series en una sola línea. Los otros 3 módulos se traen aparte
      // abajo (equityWeatherRes/equityPolymarketRes/equityMlbRes).
      supabase.from("equity_history").select("ts,equity").eq("module", "crypto").order("ts", { ascending: false }).limit(200),
      supabase.from("equity_history").select("ts,equity").eq("module", "weather").order("ts", { ascending: false }).limit(200),
      supabase.from("equity_history").select("ts,equity").eq("module", "polymarket").order("ts", { ascending: false }).limit(200),
      supabase.from("equity_history").select("ts,equity").eq("module", "mlb").order("ts", { ascending: false }).limit(200),
      supabase.from("decisions").select("*").order("ts", { ascending: false }).limit(30),
      supabase.from("bot_state").select("*"),
      supabase.from("pending_decisions").select("*").eq("resolved", false),
      // NUEVO: posiciones cripto abiertas (modo papel) — antes run_cycle()
      // nunca las registraba, así que esta tabla estaba siempre vacía.
      supabase.from("open_trades").select("*").order("ts_opened", { ascending: false }).limit(OPEN_ROWS_LIMIT),
      // NUEVO (08/09/2026): confidence sumado al select -- hacía falta para
      // computeStatsByConfidence() más abajo. setup_type no se pide porque
      // todavía no tiene ningún trade cerrado con ese dato (columna
      // agregada el mismo día que esto).
      supabase.from("closed_trades").select("outcome,r_multiple,confidence").order("ts_closed", { ascending: false }).limit(500),
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
      equity_weather: equityWeatherRes,
      equity_polymarket: equityPolymarketRes,
      equity_mlb: equityMlbRes,
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

    // FIX (06/09/2026): EXCLUDED_CATEGORIES solo se aplicaba al indicador
    // agregado (resolvedSignalsCore) y a polymarket_stats_by_category —
    // "Señales abiertas" e "Historial reciente" mandaban las señales tal
    // cual venían de Supabase, sin categoría ni marca de exclusión, así
    // que una categoría excluida (ej. Clima) seguía apareciendo ahí como
    // si el filtro no existiera. Se le agrega `category` a cada fila para
    // que el frontend pueda ocultarlas/marcarlas igual que ya hace en la
    // tabla de performance por categoría.
    const polymarketOpenRows = (polymarketOpenRes.error ? [] : (polymarketOpenRes.data || []))
      .map((r) => ({ ...r, category: categorize(r.question) }));
    const resolvedSignalsWithCategory = resolvedSignals.map((r) => ({ ...r, category: categorize(r.question) }));

    return NextResponse.json({
      equity: (equityRes.data || []).slice().reverse(),
      // AUDITORÍA (07/09/2026, pedido del usuario): series de equity por
      // módulo aparte de cripto (ver apply_binary_signal_pnl/
      // apply_r_multiple_pnl en supabase_db.py) -- cada una arranca en $100
      // y solo tiene puntos a partir de la primera señal resuelta de ese
      // módulo, así que pueden llegar vacías por un rato.
      equity_weather: equityWeatherRes.error ? [] : (equityWeatherRes.data || []).slice().reverse(),
      equity_polymarket: equityPolymarketRes.error ? [] : (equityPolymarketRes.data || []).slice().reverse(),
      equity_mlb: equityMlbRes.error ? [] : (equityMlbRes.data || []).slice().reverse(),
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
      stats: closedTradesRes.error ? { n: 0, win_rate: null, expectancy_r: null, profit_factor: null, breakdown: null }
        : computeStats(closedTradesRes.data),
      polymarket_stats: polymarketResolvedRes.error ? { n: 0, win_rate: null, avg_return_pct: null, expectancy_r: null, profit_factor: null }
        : computePolymarketStats(resolvedSignalsCore),
      polymarket_stats_all_categories: polymarketResolvedRes.error ? { n: 0, win_rate: null, avg_return_pct: null, expectancy_r: null, profit_factor: null }
        : computePolymarketStats(resolvedSignals),
      polymarket_excluded_categories: EXCLUDED_CATEGORIES,
      polymarket_stats_by_category: computePolymarketStatsByCategory(resolvedSignals),
      polymarket_open: polymarketOpenRows,
      polymarket_resolved: resolvedSignalsWithCategory.slice(0, 20),
      indicators: indicatorsRes.error ? [] : Object.values(latestIndicatorsBySymbol),
      weather_open: weatherOpenRes.error ? [] : (weatherOpenRes.data || []),
      weather_resolved: weatherResolved.slice(0, 20),
      weather_stats: weatherResolvedRes.error ? { n: 0, win_rate: null, avg_return_pct: null, brier_score: null }
        : computeWeatherStats(weatherResolved),
      mlb_open: mlbOpenRes.error ? [] : (mlbOpenRes.data || []),
      mlb_resolved: mlbResolved.slice(0, 20),
      mlb_stats: mlbResolvedRes.error ? { n: 0, win_rate: null, avg_return_pct: null, brier_score: null }
        : computeMlbStats(mlbResolved),
      mlb_calibration: mlbResolvedRes.error ? { n: 0, buckets: [] } : computeMlbCalibration(mlbResolved),
      weather_calibration: weatherResolvedRes.error ? { n: 0, buckets: [] } : computeWeatherCalibration(weatherResolved),
      crypto_stats_by_confidence: closedTradesRes.error ? {} : computeStatsByConfidence(closedTradesRes.data || []),
      polymarket_stats_by_confidence: polymarketResolvedRes.error ? {} : computePolymarketStatsByConfidence(resolvedSignalsCore),
    });
  } catch (e) {
    return NextResponse.json({ error: String(e) }, { status: 500 });
  }
          }
