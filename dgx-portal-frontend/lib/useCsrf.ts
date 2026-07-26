"use client";

import { useEffect, useState } from "react";
import { fetchCsrfToken } from "./api";

export function useCsrf(): string {
  const [csrf, setCsrf] = useState("");
  useEffect(() => {
    fetchCsrfToken().then(setCsrf).catch(() => {});
  }, []);
  return csrf;
}
