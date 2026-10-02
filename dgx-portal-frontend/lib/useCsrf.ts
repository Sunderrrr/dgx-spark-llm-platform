"use client";

import { useContext } from "react";
import { CsrfContext } from "./csrf";

// The token is fetched ONCE by CsrfProvider (mounted at the root) and shared
// by every consumer through context. Each call used to issue its own
// /api/csrf request (13 call sites); now it is a single read.
export function useCsrf(): string {
  return useContext(CsrfContext).csrf;
}

// The refresh, for the few places that must guarantee a fresh token right
// before sending: logout, where a CSRF 400 is a dead end (the user can no
// longer log out). Everywhere else the provider's token is enough and a
// context read is cheaper.
export function useCsrfRefresh(): () => Promise<string> {
  return useContext(CsrfContext).refresh;
}
