"use client";

import { useEffect, useState } from "react";

/** `prefers-reduced-motion: reduce`, lu côté client.
 *
 *  Pourquoi un hook et pas une simple requête média dans le CSS : l'avatar
 *  généré est un SVG ANIMÉ passé en `src` d'<img>. Mesuré dans Chromium, une
 *  data-URI chargée comme image n'évalue PAS `prefers-reduced-motion` — son
 *  animation CSS comme son SMIL continuent de tourner. C'est donc au composant
 *  de demander la variante fixe.
 *
 *  SSR-safe : faux au premier rendu (comme le serveur), corrigé après montage.
 *  Lire la préférence dans l'initialiseur ferait diverger le `src` du HTML
 *  servi et celui du premier rendu client — une erreur d'hydratation. */
export function useMouvementReduit(): boolean {
  const [reduit, setReduit] = useState(false);
  useEffect(() => {
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setReduit(mq.matches);
    update();
    mq.addEventListener("change", update);
    return () => mq.removeEventListener("change", update);
  }, []);
  return reduit;
}
