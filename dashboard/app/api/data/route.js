import { NextResponse } from "next/server";
import { createClient } from "@supabase/supabase-js";

function getClient() {
  return createClient(process.env.SUPABASE_URL, process.env.SUPABASE_SERVICE_KEY);
}

export async function GET() {
  try {
    const supabase = getClient();

    const [equityRes, decisionsRes, stateRes, pendingRes] = await Promise.all([
      supabase.from("equity_history").select("ts,equity").order("ts", { ascending: true }).limit(200),
      supabase.from("decisions").select("*").order("ts", { ascending: false }).limit(30),
      supabase.from("bot_state").select("*"),
      supabase.from("pending_decisions").select("*").eq("resolved", false),
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
    });
  } catch (e) {
    return NextResponse.json({ error: String(e) }, { status: 500 });
  }
}
