"use client";
import { useEffect, useState } from "react";

function EquitySparkline({ points }) {
  if (!points || points.length < 2) {
    return <div className="empty">Todavía no hay suficiente historial de equity.</div>;
  }
  const values = points.map((p) => p.equity);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const w = 600, h = 120, pad = 8;
  const norm = points
    .map((p, i) => {
      const x = pad + (i / (points.length - 1)) * (w - pad * 2);
      const y = h - pad - ((p.equity - min) / (max - min || 1)) * (h - pad * 2);
      return `${x},${y}`;
    })
    .join(" ");
  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="equity-chart" preserveAspectRatio="none">
      <polyline points={norm} fill="none" stroke="#E15A2C" strokeWidth="2" />
    </svg>
  );
}

export default function Dashboard() {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");

  async function load() {
    try {
      const res = await fetch("/api/data", { cache: "no-store" });
      const json = await res.json();
      if (!res.ok) throw new Error(json.error || "No se pudo cargar el panel");
      setData(json);
      setError("");
    } catch (e) {
      setError(e.message);
    }
  }

  useEffect(() => {
    load();
    const id = setInterval(load, 15000); // refresco cada 15s
    return () => clearInterval(id);
  }, []);

  if (error) {
    return (
      <div className="wrap">
        <p className="error">{error}</p>
      </div>
    );
  }
  if (!data) {
    return (
      <div className="wrap">
        <p className="label">Cargando…</p>
      </div>
    );
  }

  const lastEquity = data.equity.length ? data.equity[data.equity.length - 1].equity : null;

  return (
    <div className="wrap">
      <div className="header">
        <h1>Trader IA 24/7</h1>
        <span className={`status ${data.halted ? "halted" : "online"}`}>
          {data.halted ? `Detenido — ${data.halt_reason}` : "Sistema en línea"}
        </span>
      </div>

      {data.pending.length > 0 && (
        <div className="pending-banner">
          Esperando tu respuesta en Telegram para: {data.pending.map((p) => p.symbol).join(", ")}
        </div>
      )}

      <div className="card">
        <h2>Equity</h2>
        {lastEquity !== null && <div className="equity-value">${lastEquity.toFixed(2)}</div>}
        <EquitySparkline points={data.equity} />
      </div>

      <div className="card">
        <h2>Bitácora de decisiones</h2>
        {data.decisions.length === 0 ? (
          <p className="empty">Todavía no hay decisiones registradas.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Fecha</th>
                <th>Símbolo</th>
                <th>Señal</th>
                <th>Dirección</th>
                <th>Confianza</th>
                <th>Riesgo</th>
                <th>Decisión</th>
              </tr>
            </thead>
            <tbody>
              {data.decisions.map((d) => (
                <tr key={d.id}>
                  <td>{new Date(d.ts * 1000).toLocaleString()}</td>
                  <td>{d.symbol}</td>
                  <td>{d.signal_type || "—"}</td>
                  <td>{d.direction || "—"}</td>
                  <td>{d.confidence ? "★".repeat(d.confidence) : "—"}</td>
                  <td className={d.risk_pass ? "ok" : "fail"}>{d.risk_pass ? "OK" : "FALLA"}</td>
                  <td className={`decision-${d.decision}`}>{d.decision}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
