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

// Astryx rend un <a> natif par défaut : chaque clic dans la barre latérale
// rechargeait donc l'application entière au lieu de naviguer côté client.
// LinkProvider redirige TOUS les composants porteurs d'un href (SideNavItem,
// Button, Link, Breadcrumb…) vers next/link d'un seul coup.
//
// L'adaptateur n'est pas décoratif : useLinkComponent duplique href dans une
// prop `to` pour les routeurs qui l'attendent (React Router, TanStack). Next
// ne la connaît pas et la laisserait filer jusqu'au <a>, où React se plaint
// d'un attribut DOM inconnu. On l'absorbe ici.
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
  // L'état initial doit être IDENTIQUE au rendu serveur : lire localStorage
  // dans l'initialiseur ferait diverger le premier rendu client du HTML SSR
  // (erreur d'hydratation React #418, texte remplacé silencieusement). On
  // applique donc les préférences locales juste après le montage.
  const [mode, setModeState] = useState<Mode>("system");
  const [themeId, setThemeIdState] = useState<ThemeId>("neutral");
  const [lang, setLangState] = useState<Lang>("fr");

  useEffect(() => {
    setModeState(readLocal<Mode>("cronos_theme_mode", "system"));
    setThemeIdState(readLocal<ThemeId>("cronos_theme_id", "neutral"));
    setLangState(readLocal<Lang>("cronos_lang", "fr"));
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
        {/* Traduit aussi les libellés internes d'Astryx (statuts de message,
            tri des tableaux, fermeture de dialogue…) : sans catalogue ils
            resteraient en anglais au milieu d'une interface française. */}
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
