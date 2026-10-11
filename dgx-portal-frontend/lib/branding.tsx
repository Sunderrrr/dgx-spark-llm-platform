"use client";
import { createContext, useContext, useEffect, useState, type ReactNode } from "react";

// The deployment's own identity: name in the sidebar, and the logo.
//
// « personnaliser en fonction de celui qui le déploie » — the operator names
// their own instance; « DGX » is only the default. Both values come from
// `/api/config`, which is public (the login page shows the identity too) and
// cached ten seconds server-side: one fetch per session, kept in module state
// so every shell re-render reads the same answer instead of asking again.

import { BRANDING_DEFAUT as DEFAUT, brandingFromConfig, type Branding } from "./branding-shape";

export type { Branding };

let cache: Branding | null = null;
const enAttente: ((b: Branding) => void)[] = [];
let requeteEnCours = false;

function charger() {
  if (requeteEnCours) return;
  requeteEnCours = true;
  fetch("/api/config")
    .then((r) => (r.ok ? r.json() : null))
    .then((d) => {
      cache = brandingFromConfig(d);
    })
    .catch(() => {
      cache = DEFAUT;
    })
    .finally(() => {
      requeteEnCours = false;
      for (const f of enAttente.splice(0)) f(cache as Branding);
    });
}

/** The deployment identity, with the default until the config answers.
 *  Re-renders once the answer lands, so the sidebar never sticks to a wrong
 *  name after the fetch. */
// Read by the root layout on the server, so the first paint already carries
// the instance's name and logo (no « DGX » flashing before the real name).
const BrandingContext = createContext<Branding | null>(null);

export function BrandingProvider({ initial, children }: { initial: Branding | null; children: ReactNode }) {
  return <BrandingContext.Provider value={initial}>{children}</BrandingContext.Provider>;
}

export function useBranding(): Branding {
  const seeded = useContext(BrandingContext);
  const [etat, setEtat] = useState<Branding>(seeded ?? cache ?? DEFAUT);
  useEffect(() => {
    if (seeded || cache) return;
    enAttente.push(setEtat);
    charger();
  }, [seeded]);
  return etat;
}
