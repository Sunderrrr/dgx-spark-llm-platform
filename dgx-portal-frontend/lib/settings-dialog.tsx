"use client";

import { createContext, useContext } from "react";

/** Settings dialog tabs (mirror of `Section` in SettingsDialog). */
export type SettingsSection = "account" | "usage" | "keys" | "avatar" | "appearance" | "mcp" | "skills";

// Lets any page open the Settings dialog on a specific tab
// (e.g. the home page opening "API keys"), without a dedicated /keys page.
type SettingsDialogCtx = { open: (section?: SettingsSection) => void };

export const SettingsDialogContext = createContext<SettingsDialogCtx>({ open: () => {} });

export function useSettingsDialog() {
  return useContext(SettingsDialogContext);
}
