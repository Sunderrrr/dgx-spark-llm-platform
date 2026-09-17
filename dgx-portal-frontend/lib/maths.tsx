"use client";

/**
 * Rendu des maths écrites par le modèle : `$…$`, `$$…$$` et `\(…\)`.
 *
 * POURQUOI CE DÉTOUR (diagnostiqué en production le 2026-09-17) : KaTeX ne
 * voyait jamais la formule. Le parseur markdown d'Astryx traite l'antislash
 * comme une séquence d'échappement — `$\frac{a}{b}$` lui arrive en TROIS nœuds
 * de texte (« … $e^{i », « pi} … », « frac{a}{b}$ ») et l'antislash est consommé
 * au passage. Or un `inlinePlugins` s'applique PAR NŒUD (`node.content`) : la
 * paire `$…$` n'existe dans aucun nœud, donc la formule s'affichait telle
 * quelle, DÉGRADÉE (« $frac{a}{b}$ ») — pire que pas de rendu du tout.
 *
 * On retire donc les formules du texte AVANT le parseur, en les remplaçant par
 * une marque sans caractère significatif (zone privée Unicode + numéro), puis on
 * rend KaTeX depuis la source CONSERVÉE, antislashs intacts. Les zones de code
 * (blocs délimités, `~~~`, code en ligne) sont laissées telles quelles : `$\pi$`
 * dans un bloc de code doit rester du texte.
 */
import katex from "katex";
import "katex/dist/katex.min.css";
import type { MarkdownInlinePlugin } from "@astryxdesign/core/Markdown";

export type Formule = { latex: string; display: boolean };

/** Marques de remplacement : zone privée Unicode, absentes d'un texte de modèle. */
const DEBUT = "\uE000";
const FIN = "\uE001";
const MOTIF_MARQUE = new RegExp(`${DEBUT}(\\d+)${FIN}`, "g");

/**
 * Un seul balayage, quatre alternatives :
 *   1. une zone de code (bloc délimité, bloc `~~~`, ou portion en ligne) ;
 *   2. `$$…$$`, l'affichage (peut couvrir plusieurs lignes) ;
 *   3. `$…$`, l'en-ligne — sans espace juste après le `$` ouvrant ni avant le
 *      fermant, pour ne pas confondre avec des montants (« $5 and $10 ») ;
 *   4. `\(…\)`, l'autre écriture en ligne.
 * `(?<!\\)` évite de prendre un `\$` échappé pour un délimiteur.
 */
const MOTIF = new RegExp(
  [
    "(```[\\s\\S]*?(?:```|$)|~~~[\\s\\S]*?(?:~~~|$)|`[^`\\n]*`)",
    "(?<!\\\\)\\$\\$([\\s\\S]+?)\\$\\$",
    "(?<!\\\\)\\$([^$\\n]+?)(?<!\\s)\\$",
    "\\\\\\(([\\s\\S]+?)\\\\\\)",
  ].join("|"),
  "g",
);

/** Les cinq caractères qui, rendus en HTML, doivent être échappés. */
function echapperHtml(s: string) {
  return s.replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c] as string));
}

/**
 * KaTeX en HTML, ou l'expression ÉCHAPPÉE si KaTeX abandonne.
 *
 * Le `catch` est le correctif d'une faille : sur 5000 accolades imbriquées KaTeX
 * lève « Maximum call stack size exceeded », et renvoyer l'expression BRUTE dans
 * `dangerouslySetInnerHTML` faisait ressortir vivante une balise écrite dans une
 * réponse du modèle (seule la CSP l'arrêtait). Cf. le test navigateur.
 */
export function rendreMath(formule: Formule): string {
  try {
    return katex.renderToString(formule.latex, {
      throwOnError: false, output: "html", displayMode: formule.display,
    });
  } catch {
    return echapperHtml(formule.latex);
  }
}

/** Remplace chaque formule par une marque, et renvoie la source LaTeX conservée. */
export function protegerMaths(src: string): { texte: string; formules: Formule[] } {
  const formules: Formule[] = [];
  const texte = src.replace(
    MOTIF,
    (tout: string, code?: string, affichage?: string, enLigne?: string, parenthese?: string) => {
      if (code !== undefined) return tout; // zone de code : intacte
      const latex = affichage ?? enLigne ?? parenthese ?? "";
      formules.push({ latex, display: affichage !== undefined });
      return `${DEBUT}${formules.length - 1}${FIN}`;
    },
  );
  return { texte, formules };
}

/** Le plugin qui rend les marques posées par `protegerMaths`. */
export function pluginMaths(formules: Formule[]): MarkdownInlinePlugin {
  return {
    pattern: MOTIF_MARQUE,
    render: (match, key) => (
      <span key={key} className="cronos-inline-math"
            dangerouslySetInnerHTML={{ __html: rendreMath(formules[Number(match[1])]) }} />
    ),
  };
}
