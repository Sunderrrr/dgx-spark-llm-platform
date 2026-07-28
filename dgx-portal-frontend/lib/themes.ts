import { defineTheme } from "@astryxdesign/core/theme";
import { neutralTheme } from "@astryxdesign/theme-neutral";

/** Palettes proposées dans Réglages → Apparence.
 *
 *  Chaque variante étend le thème neutre et ne change que la couleur d'accent :
 *  defineTheme regénère alors toute la famille de tokens dérivés (accent-muted,
 *  bordures, états de survol…) de façon cohérente en clair comme en sombre.
 *  C'est la voie officielle du design system — on ne surcharge jamais
 *  --color-* dans :root, ce que les conventions Astryx interdisent
 *  explicitement (voir AGENTS.md).
 *
 *  `swatch` sert uniquement à peindre la pastille de sélection : la vraie
 *  couleur appliquée vient du thème lui-même.
 */
export type ThemeId =
  | "neutral" | "indigo" | "violet" | "rose" | "ambre"
  | "emeraude" | "cyan" | "ardoise" | "brique" | "prune";

const ACCENTS: { id: ThemeId; label: string; accent: string }[] = [
  { id: "neutral", label: "Neutre", accent: "#1F1F1F" },
  { id: "indigo", label: "Indigo", accent: "#4F46E5" },
  { id: "violet", label: "Violet", accent: "#7C3AED" },
  { id: "rose", label: "Rose", accent: "#E11D48" },
  { id: "ambre", label: "Ambre", accent: "#D97706" },
  { id: "emeraude", label: "Émeraude", accent: "#059669" },
  { id: "cyan", label: "Cyan", accent: "#0891B2" },
  { id: "ardoise", label: "Ardoise", accent: "#475569" },
  { id: "brique", label: "Brique", accent: "#C2410C" },
  { id: "prune", label: "Prune", accent: "#9D174D" },
];

export const THEMES = ACCENTS.map((t) => ({
  id: t.id,
  label: t.label,
  swatch: t.accent,
  // Le thème neutre est utilisé tel quel : le redériver changerait
  // subtilement ses gris par rapport au thème d'origine.
  theme:
    t.id === "neutral"
      ? neutralTheme
      : defineTheme({
          name: `cronos-${t.id}`,
          extends: neutralTheme,
          color: { accent: t.accent },
        }),
}));

export function themeById(id: string | null | undefined) {
  return THEMES.find((t) => t.id === id) ?? THEMES[0];
}
