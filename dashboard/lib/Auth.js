// Sesión del dashboard sin estado (no hay tabla de sesiones en Supabase):
// el token es "<timestamp>.<hmac-hex>", firmado con DASHBOARD_PASSWORD
// como clave. Solo quien conoce esa variable de entorno puede generar o
// validar un token válido, y la cookie nunca contiene la contraseña en
// sí — si se filtrara la cookie, no expone la contraseña real ni sirve
// después de que expire.
//
// Usa Web Crypto (crypto.subtle) en vez de node:crypto a propósito: este
// módulo lo importan tanto api/login/route.js (runtime Node.js) como
// middleware.js (runtime Edge, donde node:crypto no está disponible).
// crypto.subtle sí funciona en ambos runtimes.

const SESSION_MAX_AGE_MS = 30 * 24 * 60 * 60 * 1000; // 30 días — igual al maxAge de la cookie

async function importKey(secret) {
  return crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign", "verify"]
  );
}

function toHex(buffer) {
  return Array.from(new Uint8Array(buffer))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

function fromHex(hex) {
  if (typeof hex !== "string" || hex.length % 2 !== 0 || !/^[0-9a-f]*$/i.test(hex)) return null;
  const bytes = new Uint8Array(hex.length / 2);
  for (let i = 0; i < bytes.length; i++) bytes[i] = parseInt(hex.substr(i * 2, 2), 16);
  return bytes;
}

export async function createSessionToken(secret) {
  const ts = Date.now().toString();
  const key = await importKey(secret);
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(ts));
  return `${ts}.${toHex(sig)}`;
}

export async function verifySessionToken(token, secret) {
  if (!token || !secret) return false;
  const parts = token.split(".");
  if (parts.length !== 2) return false;
  const [ts, sigHex] = parts;

  const age = Date.now() - Number(ts);
  if (!Number.isFinite(age) || age < 0 || age > SESSION_MAX_AGE_MS) return false;

  const sigBytes = fromHex(sigHex);
  if (!sigBytes) return false;

  const key = await importKey(secret);
  // subtle.verify hace la comparación de la firma en tiempo constante
  // internamente — no hace falta (ni conviene) comparar los bytes a mano.
  return crypto.subtle.verify("HMAC", key, sigBytes, new TextEncoder().encode(ts));
}

// Comparación en tiempo constante para la contraseña del login (no es un
// HMAC, es texto plano contra texto plano, así que no aplica subtle.verify).
// No es perfecta — el chequeo de longitud filtra si difieren en tamaño —
// pero evita el caso más obvio: revelar cuántos caracteres iniciales
// coinciden comparando byte a byte con return temprano.
export function constantTimeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string") return false;
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) {
    diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return diff === 0;
}
