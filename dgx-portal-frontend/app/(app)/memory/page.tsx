"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useSettingsDialog } from "@/lib/settings-dialog";

/**
 * La mémoire n'a plus de page : elle est devenue un onglet des réglages. Cette
 * route ne survit que pour les liens déjà ouverts ou mis en favori — sans elle,
 * `/memory` n'est plus servi par Next, la requête part au backend Flask et
 * l'utilisateur tombe sur un « Not Found » brut au lieu de l'application.
 * On renvoie à l'accueil en ouvrant directement le bon onglet.
 */
export default function MemoryRedirect() {
  const router = useRouter();
  const { open } = useSettingsDialog();

  useEffect(() => {
    router.replace("/");
    open("memory");
    // eslint-disable-next-line react-hooks/exhaustive-deps -- une seule fois, au montage
  }, []);

  return null;
}
