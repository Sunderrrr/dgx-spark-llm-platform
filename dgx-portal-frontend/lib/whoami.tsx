"use client";

// Single source of truth for the authenticated identity. The sidebar, the home
// page and the theme/lang reconciliation each used to fetch /api/whoami on
// their own mount — three round-trips for the SAME data on every page load.
// This provider fetches it once (per page load) and hands the value down via
// context, so a request that was triplicated becomes a single one.
import { createContext, useContext, useEffect, useState, type Dispatch, type SetStateAction } from "react";

export type Whoami = {
  username: string;
  fullname: string;
  is_admin: boolean;
  avatar_id: string | null;
  theme_id: string;
  lang: string;
  onboarded: boolean;
  maintenance_mode: boolean;
};

const WhoamiContext = createContext<{
  who: Whoami | null;
  setWho: Dispatch<SetStateAction<Whoami | null>>;
} | null>(null);

export function WhoamiProvider({ children }: { children: React.ReactNode }) {
  const [who, setWho] = useState<Whoami | null>(null);

  useEffect(() => {
    // Deliberately NOT getJSON/authFetch: /api/whoami is identity, not a page's
    // data. The login page (no session) must not bounce to /login on a 401;
    // the authenticated pages already redirect when their own data fetches 401.
    fetch("/api/whoami", { credentials: "include" })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => setWho(d as Whoami | null))
      .catch(() => {});
  }, []);

  return <WhoamiContext.Provider value={{ who, setWho }}>{children}</WhoamiContext.Provider>;
}

export function useWhoami() {
  const ctx = useContext(WhoamiContext);
  if (!ctx) throw new Error("useWhoami must be used within WhoamiProvider");
  return ctx;
}
