"use client";

/**
 * Public surface of the i18n module: the provider, the hooks and the
 * server-sentence translator. `lib/i18n.tsx` re-exports exactly this list so
 * that every existing import of `@/lib/i18n` keeps working untouched.
 *
 * The dictionary lives in `./dictionary.ts`; it is intentionally not
 * re-exported here — callers use `t()` / `useT()` instead.
 */
export type { Lang } from "./context";
export { I18nProvider, useLang, useT, useLocale } from "./context";
export { tServer } from "./server";
