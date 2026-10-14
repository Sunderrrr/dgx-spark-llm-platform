"use client";
import { Text } from "@astryxdesign/core/Text";
import { useT } from "@/lib/i18n";

/* Trend: the series is computed server-side, one value per bucket of the
 * period (24 h, 7 d, 30 d or 12 months). Under 5 points we display nothing
 * rather than a curve: « depuis le début » has only 3, and three points say
 * no more than the total. */
export function Trend({ points, myself }: { points: number[]; myself: boolean }) {
  const t = useT();
  if (points.length < 5) {
    return <Text type="supporting" color="secondary">—</Text>;
  }
  const W = 100;
  const H = 24;
  const max = Math.max(...points, 1);
  const d = points
    .map((v, i) => {
      const x = (i / (points.length - 1)) * W;
      const y = H - 1 - (v / max) * (H - 2);
      return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  return (
    <svg viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", height: H, display: "block" }}
      preserveAspectRatio="none" role="img" aria-label={t("Activité sur la période")}>
      <path d={d} style={{
        fill: "none",
        stroke: myself ? "var(--color-accent)" : "var(--color-text-secondary)",
        strokeWidth: 1.5, strokeLinejoin: "round", strokeLinecap: "round",
      }} />
    </svg>
  );
}
