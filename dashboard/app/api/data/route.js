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

function computePolymarketStats(rows) {
  const resolved = (rows || []).filter((r) => r.outcome);
  if (resolved.length === 0) return { n: 0, win_rate: null };
  const wins = resolved.filter((r) => r.outcome === "target");
  return { n: resolved.length, win_rate: (wins.length / resolved.length) * 100 };
}

export async function GET() {
  try {
    const supabase = getClient();

    const [equityRes, decisionsRes, stateRes, pendingRes, closedTradesRes, polymarketRes] = await Promise.all([
      supabase.from("equity_history").select("ts,equity").order("ts", { ascending: true }).limit(200),
      supabase.from("decisions").select("*").order("ts", { ascending: false }).limit(30),
      supabase.from("bot_state").select("*"),
      supabase.from("pending_decisions").select("*").eq("resolved", false),
      // NUEVO: trades cerrados con resultado real, para calcular win rate/expectancy
      // en vez de mostrar solo la bitácora cruda de decisiones.
      supabase.from("closed_trades").select("outcome,r_multiple").order("ts_closed", { ascending: false }).limit(500),
      supabase.from("polymarket_signals").select("outcome").order("ts_signaled", { ascending: false }).limit(500),
    ]);

    const firstError = [equityRes, decisionsRes, stateRes, pendingRes].find((r) => r.error)?.error;
    if (firstError) {
      return NextResponse.json({ error: firstError.message }, { status: 500 });
    }

    const stateMap = Object.fromEntries((stateRes.data || []).map((r) => [r.key, r.value]));

    return NextResponse.json({
      equity: equityRes.data || [],
      decisions: decisionsRes.data || [],
      halted: stateMap.trading_halted === "1",
      halt_reason: stateMap.halt_reason || null,
      pending: pendingRes.data || [],
      // Si las tablas closed_trades/polymarket_signals todavía no existen
      // (schema.sql viejo sin correr de nuevo), no rompemos el dashboard —
      // simplemente se muestran las tarjetas de stats vacías.
      stats: closedTradesRes.error ? { n: 0, win_rate: null, expectancy_r: null, profit_factor: null }
        : computeStats(closedTradesRes.data),
      polymarket_stats: polymarketRes.error ? { n: 0, win_rate: null }
        : computePolymarketStats(polymarketRes.data),
    });
  } catch (e) {
    return NextResponse.json({ error: String(e) }, { status: 500 });
  }
}
