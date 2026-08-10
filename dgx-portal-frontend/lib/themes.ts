import { defineTheme } from "@astryxdesign/core/theme";
import { neutralTheme } from "@astryxdesign/theme-neutral";

/** Palettes offered in Settings → Appearance.
 *
 *  Each variant extends the neutral theme and changes only the accent color:
 *  defineTheme then regenerates the whole family of derived tokens (accent-muted,
 *  borders, hover states…) consistently in light as in dark mode.
 *  It's the design system's official path — we never override
 *  --color-* in :root, which the Astryx conventions explicitly
 *  forbid (see AGENTS.md).
 *
 *  `swatch` only serves to paint the selection dot: the real
 *  applied color comes from the theme itself.
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
  // The neutral theme is used as-is: re-deriving it would subtly
  // change its grays compared to the original theme.
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
