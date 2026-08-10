"use client";

import { Text } from "@astryxdesign/core/Text";
import { useT } from "@/lib/i18n";

export type ActivityDay = { date: string; tokens: number };

/** "Contributions"-style grid: one column per week, 7 rows (Mon→Sun).
 *  SVG rather than a grid of components: ~180 cells, and the design system
 *  has no primitive for this. Colors taken from theme tokens to follow
 *  light/dark mode. */
export function ActivityHeatmap({ days }: { days: ActivityDay[] }) {
  const translate = useT();
  if (!days.length) return null;

  const CELL = 11;
  const GAP = 3;
  const step = CELL + GAP;

  // Aligns the first column on a Monday so the rows are consistent
  // weekdays.
  const firstDow = (new Date(days[0].date + "T00:00:00").getDay() + 6) % 7;
  const cells = [...Array(firstDow).fill(null), ...days];
  const weeks = Math.ceil(cells.length / 7);

  const max = Math.max(...days.map((d) => d.tokens), 1);
  // Logarithmic scale: without it a spike crushes all the other days.
  const level = (t: number) => (t <= 0 ? 0 : Math.min(4, 1 + Math.floor((Math.log10(t) / Math.log10(max)) * 3.99)));
  const fill = [
    "var(--color-background-muted)",
    "var(--color-accent-muted)",
    "var(--color-accent-muted)",
    "var(--color-accent)",
    "var(--color-accent)",
  ];
  const opacity = [1, 0.55, 1, 0.65, 1];

  return (
    <svg
      viewBox={`0 0 ${weeks * step} ${7 * step}`}
      style={{ width: "100%", height: "auto", display: "block" }}
      preserveAspectRatio="xMinYMid meet"
      role="img"
      aria-label={translate("Activité de tokens par jour")}>
      {cells.map((d, i) => {
        if (!d) return null;
        const lvl = level(d.tokens);
        return (
          // Native SVG <title> and not the Tooltip component: the latter
          // inserts HTML, invalid inside an <svg> — the browser stripped
          // it, and the grid appeared empty.
          <rect
            key={d.date}
            x={Math.floor(i / 7) * step}
            y={(i % 7) * step}
            width={CELL}
            height={CELL}
            rx={2}
            fill={fill[lvl]}
            opacity={opacity[lvl]}
            stroke="var(--color-border)"
            strokeWidth={lvl === 0 ? 1 : 0}>
            <title>{`${new Date(d.date + "T00:00:00").toLocaleDateString("fr-FR")} — ${d.tokens.toLocaleString("fr-FR")} tokens`}</title>
          </rect>
        );
      })}
    </svg>
  );
}

export function HeatmapLegend() {
  const t = useT();
  return (
    <Text type="supporting" color="secondary">
      {t("Moins → Plus")}
    </Text>
  );
}
