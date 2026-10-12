// The admin tabs, shared by the server page (it reads `?tab=`) and the client
// component (a constant exported from a "use client" module is only a
// reference on the server side, so it lives here).

/** The `id` is what the URL carries (`?tab=models`) so an operator can share a
 *  link straight to a tab; unknown ids fall back to Vue d'ensemble. */
export const TABS = ["overview", "models", "users", "requests", "config", "system"] as const;
export type TabId = (typeof TABS)[number];
export const DEFAULT_TAB: TabId = "overview";

/** The tab a `?tab=` value designates (the default for anything unknown). */
export function tabFrom(raw: string | null | undefined): TabId {
  return raw && (TABS as readonly string[]).includes(raw) ? (raw as TabId) : DEFAULT_TAB;
}
