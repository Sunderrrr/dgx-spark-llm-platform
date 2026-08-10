"use client";

import { createContext, useContext, useEffect, useState } from "react";
import NextLink from "next/link";
import { Theme } from "@astryxdesign/core/theme";
import { LinkProvider } from "@astryxdesign/core/Link";
import { InternationalizationProvider } from "@astryxdesign/core";
import { themeById, type ThemeId } from "@/lib/themes";
import { I18nProvider, type Lang } from "@/lib/i18n";
import astryxFr from "@/lib/astryx-fr.json";

type Mode = "light" | "dark" | "system";

// Astryx renders a native <a> by default: every click in the sidebar would
// therefore reload the whole app instead of navigating client-side.
// LinkProvider redirects EVERY href-bearing component (SideNavItem,
// Button, Link, Breadcrumb…) to next/link in one shot.
//
// The adapter isn't decorative: useLinkComponent duplicates href into a
// `to` prop for routers that expect it (React Router, TanStack). Next
// doesn't know it and would let it through to the <a>, where React complains
// about an unknown DOM attribute. We absorb it here.
function NextLinkAdapter({ to, ...props }: React.ComponentProps<typeof NextLink> & { to?: string }) {
  void to;
  return <NextLink {...props} />;
}

const ThemeModeContext = createContext<{
  mode: Mode;
  setMode: (m: Mode) => void;
  themeId: ThemeId;
  setThemeId: (t: ThemeId) => void;
}>({ mode: "system", setMode: () => {}, themeId: "neutral", setThemeId: () => {} });

export function useThemeMode() {
  return useContext(ThemeModeContext);
}

// Light/dark mode stays purely local: it's a frequent toggle, no need for a
// server round-trip. The palette and language are account preferences —
// read first from localStorage to avoid a flash on first render, then
// reconciled against /api/whoami, which is authoritative from one browser
// or machine to another.
function readLocal<T extends string>(key: string, fallback: T): T {
  if (typeof window === "undefined") return fallback;
  return (window.localStorage.getItem(key) as T | null) || fallback;
}

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  // The initial state must be IDENTICAL to the server render: reading
  // localStorage in the initializer would make the first client render
  // diverge from the SSR HTML (React hydration error #418, text silently
  // replaced). So we apply the local preferences just after mount.
  const [mode, setModeState] = useState<Mode>("system");
  const [themeId, setThemeIdState] = useState<ThemeId>("neutral");
  const [lang, setLangState] = useState<Lang>("en");

  useEffect(() => {
    // One-time hydration from localStorage, just after mount: this is the
    // legitimate "sync from an external system" case (the
    // react-hooks/set-state-in-effect lint targets render cascades, not this —
    // reading localStorage in the initializer would make the SSR render
    // diverge, cf. comment above). So we disable the rule on these three lines.
    /* eslint-disable react-hooks/set-state-in-effect */
    setModeState(readLocal<Mode>("cronos_theme_mode", "system"));
    setThemeIdState(readLocal<ThemeId>("cronos_theme_id", "neutral"));
    setLangState(readLocal<Lang>("cronos_lang", "en"));
    /* eslint-enable react-hooks/set-state-in-effect */
  }, []);

  useEffect(() => {
    fetch("/api/whoami", { credentials: "include" })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (!d) return;
        if (d.theme_id) {
          setThemeIdState(d.theme_id);
          window.localStorage.setItem("cronos_theme_id", d.theme_id);
        }
        if (d.lang) {
          setLangState(d.lang);
          window.localStorage.setItem("cronos_lang", d.lang);
        }
      })
      .catch(() => {});
  }, []);

  function setMode(m: Mode) {
    setModeState(m);
    window.localStorage.setItem("cronos_theme_mode", m);
  }

  function setThemeId(t: ThemeId) {
    setThemeIdState(t);
    window.localStorage.setItem("cronos_theme_id", t);
  }

  function setLang(l: Lang) {
    setLangState(l);
    window.localStorage.setItem("cronos_lang", l);
  }

  return (
    <ThemeModeContext.Provider value={{ mode, setMode, themeId, setThemeId }}>
      <I18nProvider lang={lang} setLang={setLang}>
        {/* Also translates Astryx's internal labels (message statuses,
            table sorting, dialog close…): without a catalog they would
            stay in English in the middle of a French interface. */}
        <InternationalizationProvider locale={lang} messages={{ fr: astryxFr }}>
          <LinkProvider component={NextLinkAdapter}>
            <Theme theme={themeById(themeId).theme} mode={mode}>
              {children}
            </Theme>
          </LinkProvider>
        </InternationalizationProvider>
      </I18nProvider>
    </ThemeModeContext.Provider>
  );
}
