"use client";

/**
 * Entry point kept at this path on purpose: about 200 files import
 * `@/lib/i18n`, and that contract must not change.
 *
 * The implementation lives in `lib/i18n/`:
 *   - `dict-*.ts`  one module per domain, French msgid → English text;
 *   - `dictionary.ts`  assembles the dictionary;
 *   - `context.tsx`  provider, `useLang`, `useT`, `useLocale`;
 *   - `server.ts`  `tServer`, the display-time translator for server sentences;
 *   - `index.ts`  re-exports the public surface.
 */
export type { Lang } from "./i18n/index";
export { I18nProvider, useLang, useT, useLocale } from "./i18n/index";
export { tServer } from "./i18n/index";
