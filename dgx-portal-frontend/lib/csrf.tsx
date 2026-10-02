"use client";

// /api/csrf is fetched once here and handed down via context. `useCsrf()`
// (see lib/useCsrf.ts) now just reads this value, so a single page load no
// longer runs one /api/csrf round-trip per consumer — the layout, the settings
// dialog and every page were each fetching the same session token.
import { createContext, useCallback, useEffect, useState, type ReactNode } from "react";
import { fetchCsrfToken } from "./api";

export type CsrfCtx = { csrf: string; refresh: () => Promise<string> };

const CsrfContext = createContext<CsrfCtx>({ csrf: "", refresh: async () => "" });

export function CsrfProvider({ children }: { children: ReactNode }) {
  const [csrf, setCsrf] = useState("");

  useEffect(() => {
    let alive = true;
    // A failure here left `csrf` at "" IN SILENCE: every POST then went out
    // without a token and got « Bad Request — CSRF token manquant ou
    // invalide », with nothing indicating where the problem came from. A second
    // attempt costs ~400 ms and covers the real case: the request goes out while
    // the session is still being set up (SSO return, expiry then re-login).
    const charger = async () => {
      for (let essai = 0; essai < 2; essai += 1) {
        try {
          const t = await fetchCsrfToken();
          if (alive) setCsrf(t);
          return;
        } catch {
          if (essai === 0) await new Promise((r) => setTimeout(r, 400));
        }
      }
    };
    void charger();
    return () => {
      alive = false;
    };
  }, []);

  // The token is session-stable, so a refresh is only needed after it is
  // invalidated (e.g. a returned 401 mid-session); kept for the caller.
  const refresh = useCallback(async () => {
    const t = await fetchCsrfToken();
    setCsrf(t);
    return t;
  }, []);

  return <CsrfContext.Provider value={{ csrf, refresh }}>{children}</CsrfContext.Provider>;
}

export { CsrfContext };
