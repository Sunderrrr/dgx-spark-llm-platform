// Initial data rendered by the server, handed to a client page.
//
// A server component reads the page's data (lib/server-api.ts pageSeed) and
// passes it down as a Seed. The client page shows it at once instead of
// fetching it after boot. One subtlety: the router reuses a page's server
// payload for 30 s (next.config.ts staleTimes) and on back/forward — the SAME
// seed can come back minutes later. Each seed therefore carries a one-time id:
// the first mount that claims it skips its fetches, any later mount shows it
// and refreshes.
import { useState } from "react";

/** Per key: the endpoint's answer, or null when the server could not read it. */
export type Seed<T> = { id: string; data: T };

const claimed = new Set<string>();

function claim(seed: Seed<unknown> | null | undefined): boolean {
  if (!seed) return false;
  if (typeof window === "undefined") return true;
  if (claimed.has(seed.id)) return false;
  claimed.add(seed.id);
  return true;
}

/** The seed's data (each key null when absent), and per key whether it is
 *  FRESH — rendered for this very mount, so a mount effect may skip fetching
 *  it. Both are stable for the life of the mount. */
export function useSeed<T extends Record<string, unknown>>(
  seed: Seed<{ [K in keyof T]: T[K] | null }> | null | undefined,
): { data: { [K in keyof T]?: T[K] | null }; fresh: { [K in keyof T]?: boolean } } {
  const [state] = useState(() => {
    const isFresh = claim(seed);
    const data = (seed?.data ?? {}) as { [K in keyof T]?: T[K] | null };
    const fresh = {} as { [K in keyof T]?: boolean };
    for (const k of Object.keys(data) as (keyof T)[]) fresh[k] = isFresh && data[k] != null;
    return { data, fresh };
  });
  return state;
}
