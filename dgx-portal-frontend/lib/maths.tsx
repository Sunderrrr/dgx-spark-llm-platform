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

export type Formula = { latex: string; display: boolean };

/** Replacement markers: Unicode private use area, absent from model text. */
const START = "\uE000";
const END = "\uE001";
const BRAND_PATTERN = new RegExp(`${START}(\\d+)${END}`, "g");

/**
 * A single sweep, four alternatives:
 *   1. a code region (fenced block, `~~~` block, or inline portion) ;
 *   2. `$$…$$`, display math (can span several lines) ;
 *   3. `$…$`, inline math — no space right after the opening `$` nor before
 *      the closing one, so as not to confuse it with amounts (« $5 and $10 ») ;
 *   4. `\(…\)`, the other inline notation.
 * `(?<!\\)` avoids taking an escaped `\$` for a delimiter.
 */
const PATTERN = new RegExp(
  [
    "(```[\\s\\S]*?(?:```|$)|~~~[\\s\\S]*?(?:~~~|$)|`[^`\\n]*`)",
    "(?<!\\\\)\\$\\$([\\s\\S]+?)\\$\\$",
    "(?<!\\\\)\\$([^$\\n]+?)(?<!\\s)\\$",
    "\\\\\\(([\\s\\S]+?)\\\\\\)",
  ].join("|"),
  "g",
);

/** The five characters that, when rendered as HTML, must be escaped. */
function escapeHtml(s: string) {
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
export function renderMath(formula: Formula | undefined): string {
  // A marker whose index does NOT come from `protectMath` (model text
  // echoing \uE0000\uE001, or an out-of-bounds index) made `formulas[n]`
  // undefined: the function threw in the `try` then again in the `catch`,
  // and the exception broke the React rendering — the whole conversation,
  // replayed at every reload since it is persisted (audit 2026-10-06, F1).
  // Null-safe now, and the output stays escaped.
  const latex = String(formula?.latex ?? "");
  try {
    return katex.renderToString(latex, {
      throwOnError: false, output: "html", displayMode: formula?.display ?? false,
    });
  } catch {
    return escapeHtml(latex);
  }
}

/** Replaces each formula with a marker, and returns the kept LaTeX source. */
export function protectMath(src: string): { text: string; formulas: Formula[] } {
  const formulas: Formula[] = [];
  // The two private replacement characters are NOT legitimate text: a model
  // emitting them literally would fabricate fake markers. We remove them
  // before numbering.
  const text = src.replace(/[\uE000\uE001]/g, "").replace(
    PATTERN,
    (all: string, code?: string, display?: string, online?: string, paren?: string) => {
      if (code !== undefined) return all; // code region: untouched
      const latex = display ?? online ?? paren ?? "";
      formulas.push({ latex, display: display !== undefined });
      return `${START}${formulas.length - 1}${END}`;
    },
  );
  return { text, formulas };
}

/** The plugin that renders the markers laid by `protectMath`. */
export function mathPlugin(formulas: Formula[]): MarkdownInlinePlugin {
  return {
    pattern: BRAND_PATTERN,
    render: (match, key) => {
      const formula = formulas[Number(match[1])];
      // Out of bounds or fabricated marker: we render the text as-is rather
      // than crash (see rendreMath).
      if (!formula) return <span key={key}>{match[0]}</span>;
      return (
        <span key={key} className="dgx-inline-math"
              dangerouslySetInnerHTML={{ __html: renderMath(formula) }} />
      );
    },
  };
}
