/**
 * Avatar « généré » — la pp par défaut, créée à partir du pseudo.
 *
 * Elle est DÉTERMINISTE (même pseudo → même image, rien à stocker : l'absence
 * de logo choisi en base, `avatar_id IS NULL`, veut dire « généré ») et faite de
 * CARACTÈRES : un monogramme tiré du pseudo par-dessus une grille symétrique
 * remplie des caractères de ce même pseudo — la grille se lit comme une texture
 * à 24 px et comme des lettres à 128 px.
 *
 * Rendu en SVG dans une data-URI plutôt qu'en composant : la valeur se passe en
 * `src` d'`Avatar` (Astryx), donc tous les emplacements qui affichent une pp —
 * barre latérale, réglages, liste d'admin — gardent le même rendu, les mêmes
 * tailles et le même repli sur les initiales si l'image échoue. Aucun appel
 * réseau, donc aucun état de chargement.
 */

type Palette = {
  /** Dégradé de fond, du coin haut-gauche au coin bas-droit. */
  a: string;
  b: string;
  /** Monogramme, lisible sur le dégradé ci-dessus. */
  encre: string;
  /** Grille de caractères (même teinte que l'encre, posée très clair). */
  accent: string;
};

/**
 * Les palettes reprennent les teintes des thèmes du portail (cf. THEME_IDS dans
 * `dgx-portal/config.py`) pour que l'avatar ne jure pas avec le thème choisi.
 *
 * Chaque couple a été MESURÉ : le contraste encre/fond (WCAG, au quart, au
 * milieu et aux trois quarts du dégradé — le monogramme occupe le centre, la
 * grille déborde sur les bords) doit valoir au moins 4,5:1, le seuil du texte
 * courant. Les premières teintes, plus vives, tombaient à 2,73:1 (blanc sur
 * émeraude) : illisibles pour qui a besoin de contraste. L'ambre est le seul
 * fond clair, il reçoit donc une encre foncée.
 */
const PALETTES: Palette[] = [
  { a: "#4f46e5", b: "#6d28d9", encre: "#ffffff", accent: "#ffffff" }, // indigo 6,6:1
  { a: "#7c3aed", b: "#a21caf", encre: "#ffffff", accent: "#ffffff" }, // violet 6,0:1
  { a: "#e11d48", b: "#be185d", encre: "#ffffff", accent: "#ffffff" }, // rose 5,0:1
  { a: "#f59e0b", b: "#fbbf24", encre: "#2a1c02", accent: "#2a1c02" }, // ambre 8,2:1
  { a: "#047857", b: "#0f766e", encre: "#ffffff", accent: "#ffffff" }, // émeraude 5,5:1
  { a: "#0e7490", b: "#1d4ed8", encre: "#ffffff", accent: "#ffffff" }, // cyan 5,8:1
  { a: "#334155", b: "#475569", encre: "#ffffff", accent: "#ffffff" }, // ardoise 8,2:1
  { a: "#9a3412", b: "#dc2626", encre: "#ffffff", accent: "#ffffff" }, // brique 5,4:1
  { a: "#6b21a8", b: "#9333ea", encre: "#ffffff", accent: "#ffffff" }, // prune 6,1:1
  { a: "#0f766e", b: "#0369a1", encre: "#ffffff", accent: "#ffffff" }, // océan 5,6:1
];

/** FNV-1a 32 bits : court, stable d'un navigateur à l'autre, sans dépendance. */
function empreinte(s: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h >>> 0;
}

/** mulberry32 : suite reproductible tirée de l'empreinte (même graine → même motif). */
function melangeur(graine: number): () => number {
  let a = graine >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** « jean.dupont » → « JD », « cestienne » → « CE ». */
function monogramme(pseudo: string): string {
  const mots = pseudo.split(/[.\-_\s]+/).filter(Boolean);
  if (mots.length >= 2) return (mots[0][0] + mots[1][0]).toUpperCase();
  return pseudo.slice(0, 2).toUpperCase();
}

/** Les caractères qui remplissent la grille : ceux du pseudo, sans séparateurs. */
function lettres(pseudo: string): string {
  const propres = pseudo.replace(/[^0-9a-z]/gi, "").toUpperCase();
  return propres || "?";
}

const FONTS = "system-ui,-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif";

function echappe(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/** Grille 5×5 symétrique (moitié gauche tirée, moitié droite recopiée). */
function grille(aleatoire: () => number, caracteres: string, accent: string, anime: boolean): string {
  const cote = 5;
  const marge = 5;
  const pas = (100 - 2 * marge) / cote;
  let cellules = "";
  let i = 0;
  for (let ligne = 0; ligne < cote; ligne++) {
    for (let col = 0; col < Math.ceil(cote / 2); col++) {
      if (aleatoire() >= 0.52) continue;
      // `Set` : la colonne du milieu ne doit être écrite qu'une fois.
      for (const c of new Set([col, cote - 1 - col])) {
        const x = marge + c * pas + pas / 2;
        const y = marge + ligne * pas + pas / 2;
        const ch = caracteres[i++ % caracteres.length];
        // Le retard est NÉGATIF : l'animation démarre en plein cycle, donc la
        // vague est déjà répartie sur la grille au premier rendu (un retard
        // positif ferait s'allumer les lettres les unes après les autres).
        // Il suit la diagonale (ligne + colonne), d'où une vague qui balaie
        // l'avatar au lieu de clignoter d'un bloc.
        const delai = anime ? ` style="animation-delay:-${((ligne + c) % 6) * 0.7}s"` : "";
        const classe = anime ? ` class="v"` : "";
        // `dy` sur chaque texte, et NON sur le groupe : mesuré dans Chromium,
        // `dy` posé sur un `<g>` n'est pas hérité par ses `<text>` (le glyphe
        // remonte alors de 0,35 em, la grille se tasse vers le haut).
        cellules += `<text${classe}${delai} x="${x.toFixed(1)}" y="${y.toFixed(1)}" dy=".35em">${echappe(ch)}</text>`;
      }
    }
  }
  return `<g font-family="${FONTS}" font-size="${(pas * 0.82).toFixed(1)}" `
    + `text-anchor="middle" fill="${accent}" fill-opacity="0.28">${cellules}</g>`;
}

/**
 * Les trois mouvements de l'avatar, en CSS et en SMIL DANS le document SVG.
 *
 * Mesuré dans Chromium : dans une data-URI chargée comme `src` d'<img>, les
 * animations CSS et SMIL tournent (le script, lui, est interdit) — donc la pp
 * s'anime sans un octet de JavaScript ni de réseau. En revanche
 * `prefers-reduced-motion` n'y est PAS évalué : c'est `avatarGenere(pseudo,
 * false)` qui sert la variante fixe à qui a demandé moins de mouvement.
 *
 * Le dégradé tourne lentement (SMIL sur `gradientTransform`), les lettres de la
 * grille s'allument en vague (CSS) et le monogramme respire. L'amplitude de la
 * vague est volontairement franche : à 24 px une variation discrète ne se voit
 * pas — mesuré, la première version ne changeait que 6 pixels sur 128
 * échantillonnés, c'est-à-dire rien à l'œil. Les périodes restent longues
 * (3,6 s et 5,4 s), donc ça vit sans clignoter.
 */
const ANIMATION = `<style>@keyframes vague{0%,100%{opacity:.06}50%{opacity:.66}}`
  + `@keyframes souffle{0%,100%{opacity:1}50%{opacity:.7}}`
  + `.v{animation:vague 3.6s ease-in-out infinite}`
  + `.m{animation:souffle 5.4s ease-in-out infinite}</style>`;

const ROTATION = (duree: string) =>
  `<animateTransform attributeName="gradientTransform" type="rotate" `
  + `from="0 0.5 0.5" to="360 0.5 0.5" dur="${duree}" repeatCount="indefinite"/>`;

// Le même pseudo revient à chaque rendu (liste d'admin rechargée toutes les 8 s,
// barre latérale à chaque page) : la data-URI est calculée une fois par pseudo.
// Plafonnée : un déploiement à plusieurs milliers de comptes ne doit pas garder
// autant de chaînes en mémoire pour rien.
const CACHE = new Map<string, string>();
const CACHE_MAX = 500;

/** Data-URI de l'avatar généré pour ce pseudo. `anime: false` rend la variante
 *  fixe, pour qui a demandé moins de mouvement (`prefers-reduced-motion`). */
export function avatarGenere(pseudo: string, anime = true): string {
  const cle = (pseudo || "?").trim().toLowerCase() || "?";
  const cache = `${cle}|${anime ? "anime" : "fixe"}`;
  const connu = CACHE.get(cache);
  if (connu) return connu;

  const h = empreinte(cle);
  const aleatoire = melangeur(h);
  const pal = PALETTES[h % PALETTES.length];
  const mono = echappe(monogramme(pseudo.trim()) || "?");
  const svg =
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="128" height="128">`
    + `<defs><linearGradient id="f" x1="0" y1="0" x2="1" y2="1">`
    + `<stop offset="0" stop-color="${pal.a}"/><stop offset="1" stop-color="${pal.b}"/>`
    + (anime ? ROTATION("26s") : "")
    + `</linearGradient>`
    + (anime ? ANIMATION : "")
    + `</defs>`
    + `<rect width="100" height="100" fill="url(#f)"/>`
    + grille(aleatoire, lettres(cle), pal.accent, anime)
    + `<text${anime ? ` class="m"` : ""} x="50" y="50" font-family="${FONTS}" font-size="46" font-weight="700" `
    + `letter-spacing="-1" text-anchor="middle" dy=".35em" fill="${pal.encre}">${mono}</text>`
    + `</svg>`;
  const uri = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;

  if (CACHE.size >= CACHE_MAX) CACHE.clear();
  CACHE.set(cache, uri);
  return uri;
}

/**
 * `src` à passer à `Avatar` : le logo de marque choisi, sinon l'avatar généré.
 * Un seul point de décision, pour que les appelants n'aient pas à savoir ce que
 * « pas de logo » veut dire.
 */
export function avatarSrc(avatarId: string | null | undefined, pseudo: string, anime = true): string {
  return avatarId ? `/avatars/${avatarId}.svg` : avatarGenere(pseudo, anime);
}
