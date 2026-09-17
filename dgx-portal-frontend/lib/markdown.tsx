"use client";

/**
 * Enveloppe de `<Markdown>` qui referme le filtre d'URL de la dépendance.
 *
 * `sanitizeUrl` d'Astryx 0.1.8 (`dist/Markdown/Markdown.js:413`) teste
 * `/^(javascript|data|vbscript):/i` sur `url.trim()`. Or `trim()` ne retire que
 * les espaces de BORDURE : une tabulation ou un saut de ligne DANS le schéma
 * passe le test, alors que le navigateur, lui, supprime tabulations et sauts de
 * ligne AVANT d'analyser l'URL (algorithme WHATWG). `[clic](java\nscript:…)`
 * produit donc un lien `javascript:` bien vivant. Mesuré sur le paquet installé
 * (parseur livré, exécuté en Node) : `javascript:alert(1)` → refusé,
 * `java\nscript:alert(1)` → accepté.
 *
 * Aujourd'hui la CSP (`script-src` sans `'unsafe-inline'`, cf. `proxy.ts`)
 * empêche l'exécution — mais tout reposerait sur ce seul en-tête. On juge donc
 * ici sur l'URL NETTOYÉE de tous ses caractères de contrôle.
 *
 * Le rendu légitime reste celui du système de design : `<Link>` porte
 * exactement les mêmes classes StyleX que le lien par défaut du Markdown
 * (`xjse4m1` couleur d'accent, `x1bvjpef`), et passe par le `LinkProvider` de
 * l'application. Aucun changement visuel, et les liens externes conservent
 * `target="_blank"` + `rel="noopener noreferrer"`.
 */
import { Markdown } from "@astryxdesign/core/Markdown";
import { Link } from "@astryxdesign/core/Link";
import type { ComponentProps, ReactNode } from "react";

/** Schémas jamais légitimes dans une réponse de modèle. */
const SCHEMA_DANGEREUX = /^(?:javascript|data|vbscript|file):/i;

/**
 * URL utilisable pour un lien, ou `null` si la destination doit rester du texte.
 *
 * On retire d'abord TOUS les caractères de contrôle (U+0000–U+001F, U+007F) :
 * ce sont eux qui, invisiblement, séparent un schéma dangereux et le rendent
 * acceptable pour un `trim()`.
 */
export function hrefSur(brut: string): string | null {
  const url = String(brut ?? "").replace(/[\u0000-\u001f\u007f]/g, "").trim();
  if (!url || SCHEMA_DANGEREUX.test(url)) return null;
  return url;
}

function LienSur({ href, children }: { href: string; children: ReactNode }) {
  const url = hrefSur(href);
  // Refusé : le texte du lien reste lisible, mais il n'est plus activable.
  if (!url) return <span>{children}</span>;
  return (
    <Link href={url} isExternalLink={/^https?:\/\//i.test(url)}>
      {children}
    </Link>
  );
}

/** `<Markdown>` avec le filtre d'URL corrigé. */
export function MarkdownSur(props: ComponentProps<typeof Markdown>) {
  return <Markdown {...props} components={{ ...props.components, link: LienSur }} />;
}
