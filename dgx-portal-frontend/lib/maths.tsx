"use client";

/**
 * Rendering of math written by the model: `$…$`, `$$…$$` and `\(…\)`.
 *
 * WHY THIS DETOUR (diagnosed in production on 2026-09-17): KaTeX never
 * saw the formula. Astryx's markdown parser treats the backslash as
 * an escape sequence — `$\frac{a}{b}$` reaches it as THREE text nodes
 * (« … $e^{i », « pi} … », « frac{a}{b}$ ») and the backslash is consumed
 * along the way. But an `inlinePlugins` applies PER NODE (`node.content`):
 * the `$…$` pair exists in no node, so the formula was displayed as-is,
 * DEGRADED (« $frac{a}{b}$ ») — worse than no rendering at all.
 *
 * So we remove the formulas from the text BEFORE the parser, replacing
 * them with a marker without significant characters (Unicode private use
 * area + number), then render KaTeX from the KEPT source, backslashes
 * intact. Code regions (fenced blocks, `~~~`, inline code) are left as-is:
 * `$\pi$` in a code block must stay text.
 */
import katex from "katex";
import "katex/dist/katex.min.css";
import type { MarkdownInlinePlugin } from "@astryxdesign/core/Markdown";

export type Formule = { latex: string; display: boolean };

/** Replacement markers: Unicode private use area, absent from model text. */
const DEBUT = "\uE000";
const FIN = "\uE001";
const MOTIF_MARQUE = new RegExp(`${DEBUT}(\\d+)${FIN}`, "g");

/**
 * A single sweep, four alternatives:
 *   1. a code region (fenced block, `~~~` block, or inline portion) ;
 *   2. `$$…$$`, display math (can span several lines) ;
 *   3. `$…$`, inline math — no space right after the opening `$` nor before
 *      the closing one, so as not to confuse it with amounts (« $5 and $10 ») ;
 *   4. `\(…\)`, the other inline notation.
 * `(?<!\\)` avoids taking an escaped `\$` for a delimiter.
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

/** The five characters that, when rendered as HTML, must be escaped. */
function echapperHtml(s: string) {
  return s.replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c] as string));
}

/**
 * KaTeX as HTML, or the ESCAPED expression if KaTeX gives up.
 *
 * The `catch` is the fix for a vulnerability: on 5000 nested braces KaTeX
 * throws « Maximum call stack size exceeded », and returning the RAW
 * expression in `dangerouslySetInnerHTML` let a tag written in a model
 * answer come out alive (only the CSP stopped it). See the browser test.
 */
export function rendreMath(formule: Formule | undefined): string {
  // A marker whose index does NOT come from `protegerMaths` (model text
  // echoing \uE0000\uE001, or an out-of-bounds index) made `formules[n]`
  // undefined: the function threw in the `try` then again in the `catch`,
  // and the exception broke the React rendering — the whole conversation,
  // replayed at every reload since it is persisted (audit 2026-10-06, F1).
  // Null-safe now, and the output stays escaped.
  const latex = String(formule?.latex ?? "");
  try {
    return katex.renderToString(latex, {
      throwOnError: false, output: "html", displayMode: formule?.display ?? false,
    });
  } catch {
    return echapperHtml(latex);
  }
}

/** Replaces each formula with a marker, and returns the kept LaTeX source. */
export function protegerMaths(src: string): { texte: string; formules: Formule[] } {
  const formules: Formule[] = [];
  // The two private replacement characters are NOT legitimate text: a model
  // emitting them literally would fabricate fake markers. We remove them
  // before numbering.
  const texte = src.replace(/[\uE000\uE001]/g, "").replace(
    MOTIF,
    (tout: string, code?: string, affichage?: string, enLigne?: string, parenthese?: string) => {
      if (code !== undefined) return tout; // code region: untouched
      const latex = affichage ?? enLigne ?? parenthese ?? "";
      formules.push({ latex, display: affichage !== undefined });
      return `${DEBUT}${formules.length - 1}${FIN}`;
    },
  );
  return { texte, formules };
}

/** The plugin that renders the markers laid by `protegerMaths`. */
export function pluginMaths(formules: Formule[]): MarkdownInlinePlugin {
  return {
    pattern: MOTIF_MARQUE,
    render: (match, key) => {
      const formule = formules[Number(match[1])];
      // Out of bounds or fabricated marker: we render the text as-is rather
      // than crash (see rendreMath).
      if (!formule) return <span key={key}>{match[0]}</span>;
      return (
        <span key={key} className="cronos-inline-math"
              dangerouslySetInnerHTML={{ __html: rendreMath(formule) }} />
      );
    },
  };
}
