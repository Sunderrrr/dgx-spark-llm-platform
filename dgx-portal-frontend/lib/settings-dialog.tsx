"use client";

import { createContext, useContext } from "react";

/** Onglets du dialogue Réglages (miroir de `Section` dans SettingsDialog). */
export type SettingsSection = "account" | "usage" | "keys" | "avatar" | "appearance" | "mcp" | "skills";

// Permet à n'importe quelle page d'ouvrir le dialogue Réglages sur un onglet
// précis (ex. la page d'accueil qui ouvre « Clés API »), sans page /keys dédiée.
type SettingsDialogCtx = { open: (section?: SettingsSection) => void };

export const SettingsDialogContext = createContext<SettingsDialogCtx>({ open: () => {} });

export function useSettingsDialog() {
  return useContext(SettingsDialogContext);
}
