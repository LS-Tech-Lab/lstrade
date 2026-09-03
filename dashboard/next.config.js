/** @type {import('next').NextConfig} */
const nextConfig = {
  // Panel con contraseña + cookie de sesión (ver lib/auth.js) — headers
  // básicos de hardening que antes no estaban: evita que el panel se
  // pueda embeber en un iframe de otro sitio (clickjacking), evita que el
  // navegador adivine el content-type de una respuesta, y no filtra la URL
  // completa (con la contraseña NO va en la URL, pero por las dudas) al
  // navegar a un link externo desde acá.
  async headers() {
    return [
      {
        source: "/(.*)",
        headers: [
          { key: "X-Frame-Options", value: "DENY" },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
        ],
      },
    ];
  },
};

module.exports = nextConfig;
