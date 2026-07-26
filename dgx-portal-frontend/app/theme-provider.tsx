"use client";

import { createContext, useContext, useState } from "react";
import { Theme } from "@astryxdesign/core/theme";
import { neutralTheme } from "@astryxdesign/theme-neutral/built";

type Mode = "light" | "dark" | "system";

const ThemeModeContext = createContext<{ mode: Mode; setMode: (m: Mode) => void }>({
  mode: "system",
  setMode: () => {},
});

export function useThemeMode() {
  return useContext(ThemeModeContext);
}

function loadInitialMode(): Mode {
  if (typeof window === "undefined") return "system";
  return (window.localStorage.getItem("cronos_theme_mode") as Mode | null) || "system";
}

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const [mode, setModeState] = useState<Mode>(loadInitialMode);

  function setMode(m: Mode) {
    setModeState(m);
    window.localStorage.setItem("cronos_theme_mode", m);
  }

  return (
    <ThemeModeContext.Provider value={{ mode, setMode }}>
      <Theme theme={neutralTheme} mode={mode}>
        {children}
      </Theme>
    </ThemeModeContext.Provider>
  );
}
