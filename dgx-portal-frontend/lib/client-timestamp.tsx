"use client";

// Astryx's Timestamp, rendered only once the page is hydrated.
//
// Timestamp formats with the RUNTIME's locale and clock (no locale prop): on
// the server that is Node's (« Oct 6, 2026, 4:35 PM »), in the browser the
// visitor's (« 6 oct. 2026, 16:35 »), and a relative « il y a 2 min » moves
// between the two renders anyway. Server-rendered, every timestamp was a
// hydration mismatch (React #418), which threw the server HTML away and
// re-rendered the page in the browser.
import { useSyncExternalStore, type ComponentProps } from "react";
import { Timestamp } from "@astryxdesign/core/Timestamp";

const pasDAbonnement = () => () => {};

/** False during the server render and hydration, true afterwards. */
export function useHydrated(): boolean {
  return useSyncExternalStore(pasDAbonnement, () => true, () => false);
}

export function ClientTimestamp(props: ComponentProps<typeof Timestamp>) {
  return useHydrated() ? <Timestamp {...props} /> : null;
}
