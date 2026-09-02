import { NextResponse } from "next/server";
import { createSessionToken, constantTimeEqual } from "../../../lib/auth";

// Rate limit best-effort en memoria: vive mientras la instancia
// serverless esté "caliente" y se resetea en cada cold start, y no se
// comparte entre instancias si Vercel escala a más de una. No es un
// rate limit robusto, pero frena intentos automatizados básicos sin
// agregar infraestructura nueva (Redis, tabla en Supabase, etc.). Si en
// algún momento esto no alcanza, mover el contador a una tabla en
// Supabase le da persistencia real entre invocaciones.
const attempts = new Map(); // ip -> { count, resetAt }
const MAX_ATTEMPTS = 5;
const WINDOW_MS = 5 * 60 * 1000; // 5 minutos

function isRateLimited(ip) {
  const now = Date.now();
  const entry = attempts.get(ip);
  if (!entry || now > entry.resetAt) {
    attempts.set(ip, { count: 1, resetAt: now + WINDOW_MS });
    return false;
  }
  entry.count += 1;
  return entry.count > MAX_ATTEMPTS;
}

export async function POST(request) {
  const ip = request.headers.get("x-forwarded-for")?.split(",")[0]?.trim() || "unknown";

  if (isRateLimited(ip)) {
    return NextResponse.json(
      { error: "Demasiados intentos. Probá de nuevo en unos minutos." },
      { status: 429 }
    );
  }

  const { password } = await request.json();
  const expected = process.env.DASHBOARD_PASSWORD;

  if (!expected) {
    return NextResponse.json(
      { error: "DASHBOARD_PASSWORD no está configurada en el servidor" },
      { status: 500 }
    );
  }

  if (!constantTimeEqual(password || "", expected)) {
    return NextResponse.json({ error: "Contraseña incorrecta" }, { status: 401 });
  }

  // La cookie ya no guarda la contraseña en texto plano (antes:
  // `dashboard_pw` = password). Guarda un token firmado con expiración
  // embebida — ver lib/auth.js. Si la cookie se filtra, no expone la
  // contraseña real ni sirve una vez vencida.
  const token = await createSessionToken(expected);

  const res = NextResponse.json({ ok: true });
  res.cookies.set("dashboard_session", token, {
    httpOnly: true,
    secure: true,
    sameSite: "lax",
    path: "/",
    maxAge: 60 * 60 * 24 * 30, // 30 días
  });
  return res;
}
