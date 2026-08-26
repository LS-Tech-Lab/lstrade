import "./globals.css";

export const metadata = {
  title: "Trader IA 24/7 — Panel",
  description: "Bitácora en vivo del sistema de trading",
};

export default function RootLayout({ children }) {
  return (
    <html lang="es">
      <body>{children}</body>
    </html>
  );
}
