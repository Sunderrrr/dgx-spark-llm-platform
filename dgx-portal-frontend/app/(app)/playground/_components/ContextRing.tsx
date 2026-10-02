"use client";

import { useT } from "@/lib/i18n";

// Circular context ring: gray track + arc that fills in blue as usage grows
// (amber at 80 %, red at 95 %). Astryx has no circular gauge, so we draw a
// minimal SVG (two <circle>, no <div>). Colors are tokens, not hardcoded
// values.
export function ContextRing({ used, max }: { used: number; max: number }) {
  const t = useT();
  const ratio = Math.min(1, Math.max(0, used / (max || 1)));
  const pct = Math.round(ratio * 100);
  const color =
    ratio >= 0.95 ? "var(--color-error)" : ratio >= 0.8 ? "var(--color-warning)" : "var(--color-accent)";
  const size = 18;
  const stroke = 2.5;
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const offset = c * (1 - ratio);
  return (
    <svg
      width={size}
      height={size}
      viewBox={`0 0 ${size} ${size}`}
      role="img"
      aria-label={`${t("Utilisation du contexte")} : ${pct} %`}
    >
      {/* Gray track */}
      <circle
        cx={size / 2}
        cy={size / 2}
        r={r}
        fill="none"
        stroke="var(--color-border-emphasized)"
        strokeWidth={stroke}
      />
      {/* Fill arc, starts at the top, clockwise */}
      <circle
        cx={size / 2}
        cy={size / 2}
        r={r}
        fill="none"
        stroke={color}
        strokeWidth={stroke}
        strokeLinecap="round"
        strokeDasharray={c}
        strokeDashoffset={offset}
        transform={`rotate(-90 ${size / 2} ${size / 2})`}
        style={{ transition: "stroke-dashoffset 0.3s ease, stroke 0.3s ease" }}
      />
    </svg>
  );
}
