import { NextResponse } from "next/server";

export async function POST(request) {
  const { password } = await request.json();

  if (!process.env.DASHBOARD_PASSWORD) {
    return NextResponse.json(
      { error: "DASHBOARD_PASSWORD no está configurada en el servidor" },
      { status: 500 }
    );
  }

  if (password !== process.env.DASHBOARD_PASSWORD) {
    return NextResponse.json({ error: "Contraseña incorrecta" }, { status: 401 });
  }

  const res = NextResponse.json({ ok: true });
  res.cookies.set("dashboard_pw", password, {
    httpOnly: true,
    secure: true,
    sameSite: "lax",
    path: "/",
    maxAge: 60 * 60 * 24 * 30, // 30 días
  });
  return res;
}
