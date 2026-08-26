import { NextResponse } from "next/server";

export function middleware(request) {
  const isPublic =
    request.nextUrl.pathname.startsWith("/login") ||
    request.nextUrl.pathname.startsWith("/api/login");

  if (isPublic) return NextResponse.next();

  const cookie = request.cookies.get("dashboard_pw")?.value;
  const expected = process.env.DASHBOARD_PASSWORD;

  if (expected && cookie === expected) {
    return NextResponse.next();
  }

  const loginUrl = new URL("/login", request.url);
  return NextResponse.redirect(loginUrl);
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
