/**
 * Text copying, with a fallback where `navigator.clipboard` does not exist.
 *
 * The Clipboard API requires a SECURE CONTEXT: on the LAN over HTTP
 * (`http://dgx.cronos.lan`) it is `undefined`. Everywhere the code did
 * `navigator.clipboard?.writeText(...)`, the `?.` therefore swallowed the
 * call without doing anything — visible button, click with no effect, no
 * message. It was particularly costly in the Clés API tab, whose whole
 * point is to copy a configuration snippet.
 *
 * We fall back to `execCommand`, then SAY SO if it fails again (`onEchec`):
 * a silent failure is the only unacceptable outcome here.
 */
export async function copierTexte(texte: string, onEchec?: () => void): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(texte);
      return true;
    }
  } catch {
    /* we try the fallback */
  }
  try {
    const ta = document.createElement("textarea");
    ta.value = texte;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    if (ok) return true;
  } catch {
    /* we say so below */
  }
  onEchec?.();
  return false;
}
