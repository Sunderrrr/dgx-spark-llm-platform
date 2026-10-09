// A localStorage read that survives a KEY RENAME.
//
// « rien ne doit être monolithique » extends to the browser: when a key is
// renamed (cronos.* → dgx.*), the user's theme, language, snippets and pinned
// conversations must NOT vanish. This reads the new key first and falls back
// to the old one, so an existing session keeps working while everyone
// migrates. Drop the fallback once the fleet has moved (the old keys are
// rewritten to the new name on first read).
export type CleAncienne = { nouveau: string; ancien: string };

export function lireAvecRepli(cle: CleAncienne): string | null {
  try {
    const neuf = localStorage.getItem(cle.nouveau);
    if (neuf !== null) return neuf;
    const vieux = localStorage.getItem(cle.ancien);
    if (vieux !== null) {
      // rewrite forward, so the fallback is a one-time cost
      localStorage.setItem(cle.nouveau, vieux);
      return vieux;
    }
  } catch {
    /* storage unavailable */
  }
  return null;
}

export function ecrireAvecRepli(cle: CleAncienne, valeur: string): void {
  try {
    localStorage.setItem(cle.nouveau, valeur);
  } catch {
    /* storage unavailable */
  }
}
