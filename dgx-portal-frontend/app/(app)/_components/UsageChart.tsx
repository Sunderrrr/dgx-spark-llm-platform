"use client";

import { useEffect, useRef, useState } from "react";

export function UsageChart({ points }: { points: { hour: number; tokens: number }[] }) {
  const svgRef = useRef<SVGSVGElement>(null);
  // Mesuré en direct : le viewBox doit matcher la largeur réelle du conteneur
  // pour que le tracé remplisse toute la carte — sinon le SVG (ratio fixe,
  // "meet" par défaut) se retrouve en letterbox avec du vide de chaque côté.
  const [W, setW] = useState(760);

  useEffect(() => {
    const el = svgRef.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width;
      if (w) setW(w);
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const H = 228; // 340 * 0.67 — 33% plus bas qu'avant
  const padL = 44;
  const padR = 14;
  const padT = 12;
  const padB = 28;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;
  const vals = points.map((p) => p.tokens);
  const max = Math.max(1, ...vals);
  const x = (i: number) => padL + (i / Math.max(points.length - 1, 1)) * plotW;
  const y = (v: number) => padT + plotH * (1 - v / max);
  const line = points.map((p, i) => `${i === 0 ? "M" : "L"}${x(i)},${y(p.tokens)}`).join(" ");
  const area = `${line} L${x(points.length - 1)},${padT + plotH} L${x(0)},${padT + plotH} Z`;
  // Un repère toutes les 3h (0h, 3h, ..., 21h) pour rester lisible sur 24 points.
  const hourTicks = points.filter((p) => p.hour % 3 === 0);

  return (
    <svg ref={svgRef} viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", height: H, display: "block" }}>
      {[0, 1, 2, 3].map((g) => {
        const v = (max * g) / 3;
        const yy = y(v);
        return (
          <g key={g}>
            <line x1={padL} y1={yy} x2={W - padR} y2={yy} style={{ stroke: "var(--color-border)", strokeWidth: 1 }} />
            <text x={padL - 6} y={yy + 3} textAnchor="end" style={{ fill: "var(--color-text-secondary)", fontSize: 10 }}>
              {Math.round(v)}
            </text>
          </g>
        );
      })}
      <path d={area} style={{ fill: "var(--color-accent-muted)" }} />
      <path
        d={line}
        style={{ fill: "none", stroke: "var(--color-accent)", strokeWidth: 2, strokeLinejoin: "round", strokeLinecap: "round" }}
      />
      {hourTicks.map((p) => {
        const i = points.indexOf(p);
        const xx = x(i);
        return (
          <text
            key={p.hour}
            x={xx}
            y={H - padB + 16}
            textAnchor="middle"
            style={{ fill: "var(--color-text-secondary)", fontSize: 10 }}>
            {p.hour}h
          </text>
        );
      })}
    </svg>
  );
}
