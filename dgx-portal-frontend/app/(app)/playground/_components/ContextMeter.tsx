"use client";

/** Format court des tokens : 1 234 -> « 1,2k ». Utilisé par le volet du
 *  Playground. Le composant `ContextMeter` qui vivait ici a été supprimé le
 *  2026-09-17 : plus personne ne le rendait, le même affichage ayant été réécrit
 *  en ligne dans page.tsx. */
export function fmtK(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(n >= 100000 ? 0 : 1).replace(/\.0$/, "")}k` : `${n}`;
}
