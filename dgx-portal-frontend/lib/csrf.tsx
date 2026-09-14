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
    // Un échec ici laissait `csrf` à "" EN SILENCE : tous les POST partaient
    // alors sans jeton et recevaient « Bad Request — CSRF token manquant ou
    // invalide », sans que rien n'indique d'où venait le problème. Une seconde
    // tentative coûte ~400 ms et couvre le cas réel : la requête part pendant que
    // la session se met en place (retour de SSO, expiration puis reconnexion).
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
