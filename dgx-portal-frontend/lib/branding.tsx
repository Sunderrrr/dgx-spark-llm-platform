"use client";
import { useEffect, useState } from "react";

// The deployment's own identity: name in the sidebar, and the logo.
//
// « personnaliser en fonction de celui qui le déploie » — Cronos is THIS
// operator's homelab name, not the product's. Both values come from
// `/api/config`, which is public (the login page shows the identity too) and
// cached ten seconds server-side: one fetch per session, kept in module state
// so every shell re-render reads the same answer instead of asking again.

export type Branding = {
  /** Instance name shown top left. « Cronos » is only the default. */
  name: string;
  /** URL of the uploaded logo, or null for the built-in favicon. */
  logo: string | null;
};

const DEFAUT: Branding = { name: "Cronos", logo: null };

let cache: Branding | null = null;
const enAttente: ((b: Branding) => void)[] = [];
let requeteEnCours = false;

function charger() {
  if (requeteEnCours) return;
  requeteEnCours = true;
  fetch("/api/config")
    .then((r) => (r.ok ? r.json() : null))
    .then((d) => {
      cache = { name: d?.branding?.name || DEFAUT.name, logo: d?.branding?.logo || null };
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
export function useBranding(): Branding {
  const [etat, setEtat] = useState<Branding>(cache ?? DEFAUT);
  useEffect(() => {
    if (cache) return;
    enAttente.push(setEtat);
    charger();
  }, []);
  return etat;
}
