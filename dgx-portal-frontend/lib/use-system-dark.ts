"use client";

// Whether the system asks for dark mode, safe for server rendering.
//
// The server cannot know the visitor's preference: libraries that resolve
// « auto » themselves during render (border-beam did) picked dark on the server
// and light in the browser, a hydration mismatch (React #418) that threw the
// server HTML away. This answers false during the server render and hydration,
// then the real preference, and follows its changes.
import { useSyncExternalStore } from "react";

const QUERY = "(prefers-color-scheme: dark)";

function subscribe(onChange: () => void) {
  const mq = window.matchMedia(QUERY);
  mq.addEventListener("change", onChange);
  return () => mq.removeEventListener("change", onChange);
}

export function useSystemDark(): boolean {
  return useSyncExternalStore(subscribe, () => window.matchMedia(QUERY).matches, () => false);
}

/** The app's light/dark mode with « system » resolved, hydration-safe. */
export function useResolvedMode(mode: "light" | "dark" | "system"): "light" | "dark" {
  const systemDark = useSystemDark();
  return mode === "system" ? (systemDark ? "dark" : "light") : mode;
}
