/**
 * Copie du texte, avec repli là où `navigator.clipboard` n'existe pas.
 *
 * L'API Clipboard exige un CONTEXTE SÉCURISÉ : sur le LAN en HTTP
 * (`http://dgx.cronos.lan`) elle est `undefined`. Partout où le code faisait
 * `navigator.clipboard?.writeText(...)`, le `?.` avalait donc l'appel sans rien
 * faire — bouton visible, clic sans effet, aucun message. C'était
 * particulièrement coûteux dans l'onglet Clés API, dont tout l'intérêt est de
 * copier un extrait de configuration.
 *
 * On retombe sur `execCommand`, puis on le DIT si ça échoue encore (`onEchec`) :
 * un échec silencieux est le seul résultat inacceptable ici.
 */
export async function copierTexte(texte: string, onEchec?: () => void): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(texte);
      return true;
    }
  } catch {
    /* on essaie le repli */
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
    /* on le dit plus bas */
  }
  onEchec?.();
  return false;
}
