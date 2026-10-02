"use client";

/** Short token formatting: 1 234 -> « 1,2k ». Used by the Playground
 *  panel. The `ContextMeter` component that lived here was removed on
 *  2026-09-17: nobody rendered it anymore, the same display having been
 *  rewritten inline in page.tsx. */
export function fmtK(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(n >= 100000 ? 0 : 1).replace(/\.0$/, "")}k` : `${n}`;
}
