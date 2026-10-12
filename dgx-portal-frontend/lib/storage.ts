// A localStorage read that survives a KEY RENAME.
//
// « rien ne doit être monolithique » extends to the browser: when a key is
// renamed (after a rename), the user's theme, language, snippets and pinned
// conversations must NOT vanish. This reads the new key first and falls back
// to the old one, so an existing session keeps working while everyone
// migrates. Drop the fallback once the fleet has moved (the old keys are
// rewritten to the new name on first read).
export type LegacyKey = { current: string; legacy: string };

export function readWithFallback(key: LegacyKey): string | null {
  try {
    const fresh = localStorage.getItem(key.current);
    if (fresh !== null) return fresh;
    const old = localStorage.getItem(key.legacy);
    if (old !== null) {
      // rewrite forward, so the fallback is a one-time cost
      localStorage.setItem(key.current, old);
      return old;
    }
  } catch {
    /* storage unavailable */
  }
  return null;
}

export function writeWithFallback(key: LegacyKey, value: string): void {
  try {
    localStorage.setItem(key.current, value);
  } catch {
    /* storage unavailable */
  }
}
