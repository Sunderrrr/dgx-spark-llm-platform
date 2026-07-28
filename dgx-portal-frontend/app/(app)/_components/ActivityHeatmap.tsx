"use client";

import { Text } from "@astryxdesign/core/Text";

export type ActivityDay = { date: string; tokens: number };

/** Grille type « contributions » : une colonne par semaine, 7 lignes (lun→dim).
 *  SVG plutôt qu'une grille de composants : ~180 cellules, et le design system
 *  n'a pas de primitive pour ça. Couleurs prises aux tokens de thème pour
 *  suivre le mode clair/sombre. */
export function ActivityHeatmap({ days }: { days: ActivityDay[] }) {
  if (!days.length) return null;

  const CELL = 11;
  const GAP = 3;
  const step = CELL + GAP;

  // Aligne la première colonne sur un lundi pour que les lignes soient des
  // jours de semaine cohérents.
  const firstDow = (new Date(days[0].date + "T00:00:00").getDay() + 6) % 7;
  const cells = [...Array(firstDow).fill(null), ...days];
  const weeks = Math.ceil(cells.length / 7);

  const max = Math.max(...days.map((d) => d.tokens), 1);
  // Échelle logarithmique : sans elle un pic écrase tous les autres jours.
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
      aria-label="Activité de tokens par jour">
      {cells.map((d, i) => {
        if (!d) return null;
        const lvl = level(d.tokens);
        return (
          // <title> natif SVG et non le composant Tooltip : celui-ci insère
          // du HTML, invalide à l'intérieur d'un <svg> — le navigateur le
          // supprimait, et la grille apparaissait vide.
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
  return (
    <Text type="supporting" color="secondary">
      Moins → Plus
    </Text>
  );
}
