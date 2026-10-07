// Number formatting for the settings sections. Extracted VERBATIM from
// app/(app)/_components/SettingsDialog.tsx.

// The locale is passed by the component: the formatting follows the
// displayed language and a helper outside a component cannot call a hook.
export function fmt(n: number, numLocale: string) {
  return Math.round(n).toLocaleString(numLocale);
}

/** 12,400 → "12 k": the top tiles must stay readable. */
export function compact(n: number) {
  if (n >= 1e9) return `${(n / 1e9).toFixed(1).replace(/\.0$/, "")} G`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1).replace(/\.0$/, "")} M`;
  if (n >= 1e3) return `${Math.round(n / 1e3)} k`;
  return String(Math.round(n));
}
