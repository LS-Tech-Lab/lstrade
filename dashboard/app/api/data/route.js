import { NextResponse } from "next/server";
import { createClient } from "@supabase/supabase-js";

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

// Categorías por keywords — mismas reglas que polymarket_categories.py
// (Python). Si se agrega/cambia una categoría ahí, hay que reflejarlo acá
// también: no comparten código porque uno corre en el motor de señales
// (Python) y el otro solo para agrupar en el dashboard (JS).
const CATEGORY_RULES = [
  ["Clima", /temperature|hottest|coldest|rain|snow|hurricane|heat wave|weather|degrees?\b|Fahrenheit|Celsius/i],
  ["Lanzamientos / FDV", /\bFDV\b|one day after launch|launch a token/i],
  ["Valuaciones privadas", /valuation hit|\(HIGH\)|\(LOW\)/i],
  ["Objetivo de precio cripto", /\breach \$|\bdip to \$|market cap/i],
  ["Política / elecciones", /election|senat|vote|president|confirm|governor|congress|by-election/i],
  ["Deportes / vanity", /\bfight\b|attend|wedding|\bwin\b.*(match|game|fight)|UFC/i],
  ["Macro / eventos", /bank failure|hack over|open interest/i],
  ["IA / tech", /Claude|OpenAI|Anthropic|GPT|Frontier Math|Opus|Gemini/i],
];
const FALLBACK_CATEGORY = "Otros / sin clasificar";

function categorize(question) {
  if (!question) return FALLBACK_CATEGORY;
  for (const [name, pattern] of CATEGORY_RULES) {
    if (pattern.test(question)) return name;
  }
  return FALLBACK_CATEGORY;
}

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
    ] = await Promise.all([
      supabase.from("equity_history").select("ts,equity").order("ts", { ascending: true }).limit(200),
      supabase.from("decisions").select("*").order("ts", { ascending: false }).limit(30),
      supabase.from("bot_state").select("*"),
      supabase.from("pending_decisions").select("*").eq("resolved", false),
      // NUEVO: posiciones cripto abiertas (modo papel) — antes run_cycle()
      // nunca las registraba, así que esta tabla estaba siempre vacía.
      supabase.from("open_trades").select("*").order("ts_opened", { ascending: false }),
      supabase.from("closed_trades").select("outcome,r_multiple").order("ts_closed", { ascending: false }).limit(500),
      // Señales de Polymarket todavía sin resolver — "posiciones abiertas" de ese módulo.
      supabase.from("polymarket_signals").select("*").is("outcome", null).order("ts_signaled", { ascending: false }),
      // Últimas resueltas: para el historial reciente y las stats por categoría.
      supabase.from("polymarket_signals").select("*").not("outcome", "is", null).order("ts_resolved", { ascending: false }).limit(200),
      // NUEVO: último snapshot de indicadores por símbolo (ver
      // indicator_snapshots en schema.sql) — antes el dashboard solo podía
      // mostrar RSI/tendencia en los raros ciclos donde hubo señal real.
      supabase.from("indicator_snapshots").select("*").order("ts", { ascending: false }).limit(200),
    ]);

    const firstError = [equityRes, decisionsRes, stateRes, pendingRes].find((r) => r.error)?.error;
    if (firstError) {
      return NextResponse.json({ error: firstError.message }, { status: 500 });
    }

    const stateMap = Object.fromEntries((stateRes.data || []).map((r) => [r.key, r.value]));

    // Un snapshot por símbolo (el más reciente) — la tabla puede tener
    // varias filas históricas por símbolo, acá solo interesa la última.
    const latestIndicatorsBySymbol = {};
    for (const row of indicatorsRes.data || []) {
      if (!latestIndicatorsBySymbol[row.symbol]) latestIndicatorsBySymbol[row.symbol] = row;
    }

    const resolvedSignals = polymarketResolvedRes.error ? [] : (polymarketResolvedRes.data || []);

    return NextResponse.json({
      equity: equityRes.data || [],
      decisions: decisionsRes.data || [],
      halted: stateMap.trading_halted === "1",
      halt_reason: stateMap.halt_reason || null,
      pending: pendingRes.data || [],
      // Si las tablas todavía no existen (schema.sql viejo sin correr de
      // nuevo), no rompemos el dashboard — se muestran vacías.
      crypto_open: openTradesRes.error ? [] : (openTradesRes.data || []),
      stats: closedTradesRes.error ? { n: 0, win_rate: null, expectancy_r: null, profit_factor: null }
        : computeStats(closedTradesRes.data),
      polymarket_stats: polymarketResolvedRes.error ? { n: 0, win_rate: null }
        : computePolymarketStats(resolvedSignals),
      polymarket_stats_by_category: computePolymarketStatsByCategory(resolvedSignals),
      polymarket_open: polymarketOpenRes.error ? [] : (polymarketOpenRes.data || []),
      polymarket_resolved: resolvedSignals.slice(0, 20),
      indicators: indicatorsRes.error ? [] : Object.values(latestIndicatorsBySymbol),
    });
  } catch (e) {
    return NextResponse.json({ error: String(e) }, { status: 500 });
  }
}
