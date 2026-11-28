"use client";

import { createContext, useCallback, useContext } from "react";
import { EN } from "./dictionary";

/** Languages offered in Settings → Appearance. */
export type Lang = "fr" | "en";

const I18nContext = createContext<{ lang: Lang; setLang: (l: Lang) => void }>({
  lang: "fr",
  setLang: () => {},
});

export function I18nProvider({
  lang,
  setLang,
  children,
}: {
  lang: Lang;
  setLang: (l: Lang) => void;
  children: React.ReactNode;
}) {
  return <I18nContext.Provider value={{ lang, setLang }}>{children}</I18nContext.Provider>;
}

export function useLang() {
  return useContext(I18nContext);
}

/** t("French text") — identity in FR, translation in EN. */
export function useT() {
  const { lang } = useContext(I18nContext);
  return useCallback((fr: string) => (lang === "en" ? EN[fr] ?? fr : fr), [lang]);
}

/** Formatting locale (numbers, dates, times) that FOLLOWS the displayed language.
 *
 *  The i18n contract is not only about text: a hardcoded `toLocaleString("fr-FR")`
 *  displayed « 1 234 » and « 13/09/2026 » to an English reader,
 *  who expects « 1,234 » and « 9/13/2026 ». About twenty places did this;
 *  a few components already had the right formula (`lang === "fr" ? … : …`),
 *  adopted here once and for all. Use via `const numLocale = useLocale()`.
 *  In a helper outside a component, pass the value as a parameter: a hook can
 *  only be called from a component.
 */
export function useLocale(): string {
  const { lang } = useContext(I18nContext);
  return lang === "fr" ? "fr-FR" : "en-US";
}
