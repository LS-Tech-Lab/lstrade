import { NextResponse } from "next/server";
import { verifySessionToken } from "./lib/auth";

export async function middleware(request) {
  const isPublic =
    request.nextUrl.pathname.startsWith("/login") ||
    request.nextUrl.pathname.startsWith("/api/login");

  if (isPublic) return NextResponse.next();

  // La cookie guarda un token firmado (ver lib/auth.js), no la
  // contraseña — antes era `dashboard_pw` con la contraseña en texto
  // plano.
  const token = request.cookies.get("dashboard_session")?.value;
  const secret = process.env.DASHBOARD_PASSWORD;

  if (await verifySessionToken(token, secret)) {
    return NextResponse.next();
  }

  const loginUrl = new URL("/login", request.url);
  return NextResponse.redirect(loginUrl);
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
