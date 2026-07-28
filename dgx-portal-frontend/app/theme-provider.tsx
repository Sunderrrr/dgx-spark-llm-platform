"use client";

import { createContext, useContext, useEffect, useState } from "react";
import { Theme } from "@astryxdesign/core/theme";
import { InternationalizationProvider } from "@astryxdesign/core";
import { themeById, type ThemeId } from "@/lib/themes";
import { I18nProvider, type Lang } from "@/lib/i18n";
import astryxFr from "@/lib/astryx-fr.json";

type Mode = "light" | "dark" | "system";

const ThemeModeContext = createContext<{
  mode: Mode;
  setMode: (m: Mode) => void;
  themeId: ThemeId;
  setThemeId: (t: ThemeId) => void;
}>({ mode: "system", setMode: () => {}, themeId: "neutral", setThemeId: () => {} });

export function useThemeMode() {
  return useContext(ThemeModeContext);
}

// Le mode clair/sombre reste purement local : c'est un basculement fréquent,
// pas la peine d'un aller-retour serveur. La palette et la langue sont des
// préférences de compte — lues d'abord depuis localStorage pour éviter un
// flash au premier rendu, puis recalées sur /api/whoami, qui fait autorité
// d'un navigateur ou d'une machine à l'autre.
function readLocal<T extends string>(key: string, fallback: T): T {
  if (typeof window === "undefined") return fallback;
  return (window.localStorage.getItem(key) as T | null) || fallback;
}

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const [mode, setModeState] = useState<Mode>(() => readLocal("cronos_theme_mode", "system"));
  const [themeId, setThemeIdState] = useState<ThemeId>(() =>
    readLocal<ThemeId>("cronos_theme_id", "neutral"),
  );
  const [lang, setLangState] = useState<Lang>(() => readLocal<Lang>("cronos_lang", "fr"));

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
        {/* Traduit aussi les libellés internes d'Astryx (statuts de message,
            tri des tableaux, fermeture de dialogue…) : sans catalogue ils
            resteraient en anglais au milieu d'une interface française. */}
        <InternationalizationProvider locale={lang} messages={{ fr: astryxFr }}>
          <Theme theme={themeById(themeId).theme} mode={mode}>
            {children}
          </Theme>
        </InternationalizationProvider>
      </I18nProvider>
    </ThemeModeContext.Provider>
  );
}
