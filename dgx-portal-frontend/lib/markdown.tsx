"use client";

/**
 * Wrapper around `<Markdown>` that plugs two holes in the dependency: its URL
 * filter, and math rendering.
 *
 * `sanitizeUrl` from Astryx 0.1.8 (`dist/Markdown/Markdown.js:413`) tests
 * `/^(javascript|data|vbscript):/i` against `url.trim()`. But `trim()` only
 * removes EDGE spaces: a tab or a newline INSIDE the scheme passes the
 * test, while the browser, for its part, removes tabs and newlines BEFORE
 * parsing the URL (WHATWG algorithm). `[clic](java\nscript:…)` therefore
 * produces a perfectly alive `javascript:` link. Measured on the installed
 * package (shipped parser, run in Node): `javascript:alert(1)` → refused,
 * `java\nscript:alert(1)` → accepted.
 *
 * Today the CSP (`script-src` without `'unsafe-inline'`, cf. `proxy.ts`)
 * prevents execution — but everything would rest on that single header. So
 * we judge here on the URL stripped of ALL its control characters.
 *
 * The legitimate rendering remains the design system's: `<Link>` carries
 * exactly the same StyleX classes as the default Markdown link
 * (`xjse4m1` accent color, `x1bvjpef`), and goes through the application's
 * `LinkProvider`. No visual change, and external links keep
 * `target="_blank"` + `rel="noopener noreferrer"`.
 *
 * Math (`$…$`, `$$…$$`, `\(…\)`) is prepared here, before the parser:
 * see `lib/maths.tsx` for the reason — the parser splits the text at every
 * backslash, so a per-node plugin can no longer see the formula.
 */
import { useMemo } from "react";
import { Markdown } from "@astryxdesign/core/Markdown";
import { Link } from "@astryxdesign/core/Link";
import type { ComponentProps, ReactNode } from "react";
import { pluginMaths, protegerMaths, type Formule } from "./maths";

/** Schemes never legitimate in a model answer. */
const SCHEMA_DANGEREUX = /^(?:javascript|data|vbscript|file):/i;

/**
 * URL usable for a link, or `null` if the destination must stay as text.
 *
 * We first remove ALL control characters (U+0000–U+001F, U+007F):
 * they are what invisibly split a dangerous scheme and make it
 * acceptable to a `trim()`.
 */
export function hrefSur(brut: string): string | null {
  const url = String(brut ?? "").replace(/[\u0000-\u001f\u007f]/g, "").trim();
  if (!url || SCHEMA_DANGEREUX.test(url)) return null;
  return url;
}

function LienSur({ href, children }: { href: string; children: ReactNode }) {
  const url = hrefSur(href);
  // Refused: the link text stays readable, but it is no longer clickable.
  if (!url) return <span>{children}</span>;
  return (
    <Link href={url} isExternalLink={/^https?:\/\//i.test(url)}>
      {children}
    </Link>
  );
}

/** `<Markdown>` with the fixed URL filter and rendered math. */
export function MarkdownSur({ children, inlinePlugins, ...props }: ComponentProps<typeof Markdown>) {
  const { texte, formules } = useMemo(() => {
    if (typeof children !== "string") return { texte: children, formules: [] as Formule[] };
    return protegerMaths(children);
  }, [children]);

  const plugins = useMemo(
    () => [...(formules.length ? [pluginMaths(formules)] : []), ...(inlinePlugins ?? [])],
    [formules, inlinePlugins],
  );

  return (
    <Markdown {...props} inlinePlugins={plugins} components={{ ...props.components, link: LienSur }}>
      {texte}
    </Markdown>
  );
}
