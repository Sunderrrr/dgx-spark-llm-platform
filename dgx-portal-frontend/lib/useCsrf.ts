"use client";

import { useContext } from "react";
import { CsrfContext } from "./csrf";

// The token is fetched ONCE by CsrfProvider (mounted at the root) and shared
// by every consumer through context. Each call used to issue its own
// /api/csrf request (13 call sites); now it is a single read.
export function useCsrf(): string {
  return useContext(CsrfContext).csrf;
}

// Le rafraîchissement, pour les rares endroits qui doivent garantir un jeton
// frais juste avant un envoi : la déconnexion, dont un 400 CSRF est un
// cul-de-sac (l'utilisateur ne peut plus se déconnecter). Partout ailleurs le
// jeton du provider suffit et une lecture de contexte est moins chère.
export function useCsrfRefresh(): () => Promise<string> {
  return useContext(CsrfContext).refresh;
}
