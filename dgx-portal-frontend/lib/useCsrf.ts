"use client";

import { useContext } from "react";
import { CsrfContext } from "./csrf";

// The token is fetched ONCE by CsrfProvider (mounted at the root) and shared
// by every consumer through context. Each call used to issue its own
// /api/csrf request (13 call sites); now it is a single read.
export function useCsrf(): string {
  return useContext(CsrfContext).csrf;
}
