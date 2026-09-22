"use client";

import { Avatar, resolveSize, type AvatarSize } from "@astryxdesign/core/Avatar";
import { Blobatar } from "@blobatar/react";

/**
 * La pp d'un compte : le logo de marque qu'il a choisi, sinon le blobatar généré
 * depuis son pseudo.
 *
 * Pourquoi [blobatar](https://github.com/Alain00/blobatar) plutôt qu'un dessin
 * maison : le déterminisme est un contrat vérifié (même pseudo → même créature,
 * y compris à travers les versions mineures — les tests dorment 1 312 rendus et
 * un histogramme de formes sur 20 000 graines), il n'a aucune dépendance, et son
 * animation au repos — respiration, hochement, clignement, regard — est faite de
 * variables tirées du pseudo, donc deux voisins ne s'animent pas en cadence. Il
 * n'émet ni `<defs>`, ni dégradé, ni filtre : plusieurs centaines de pp sur une
 * page ne peuvent pas entrer en collision d'identifiants. Enfin le contraste des
 * yeux sur le corps est garanti (≥ 4,5:1) à toutes les teintes.
 *
 * Le mouvement se coupe tout seul sous `prefers-reduced-motion` (règle dans
 * `blobatar/motion.css`, importé par `app/layout.tsx`) : rien à gérer ici, et
 * surtout pas une requête média dans le SVG, qui n'y serait pas évaluée.
 *
 * Un logo de marque, lui, reste un fichier servi par le portail : une `<img>`
 * via `<Avatar>`, donc rien à générer — et il ne s'anime pas, c'est un dessin
 * figé.
 */
export function UserAvatar({ avatarId, username, name, size = "sm" }: {
  avatarId?: string | null;
  username?: string | null;
  name?: string | null;
  size?: AvatarSize;
}) {
  if (avatarId) {
    return <Avatar src={`/avatars/${avatarId}.svg`} name={name || username || ""} size={size} />;
  }
  return (
    <Blobatar
      // Jamais de chaîne vide : blobatar hache ce qu'on lui donne, et « ? » est
      // déjà le repli des monogrammes.
      name={username || name || "?"}
      // Le SVG veut des pixels ; `resolveSize` est la fonction qui traduit
      // l'échelle du design system (xsm 20, sm 24, md 36, lg 48, xl 128) — la
      // recopier ici la ferait diverger au premier changement de jeton.
      size={resolveSize(size)}
      // Pastille pleine : la pp occupe le rond, exactement comme l'`Avatar`
      // d'Astryx à côté duquel elle s'affiche.
      background="circle"
      // « always » et non « hover » : c'est la pp de quelqu'un, elle doit vivre
      // là où on la regarde — barre latérale, réglages, liste d'admin, qui est
      // paginée à dix lignes. « hover » est le conseil de la librairie pour une
      // grille de plusieurs centaines, ce que cette page n'est pas.
      animate="always"
      title={name || username || ""}
      // `display: block` : un SVG en ligne est en boîte « inline » par défaut, et
      // laisserait l'espace de la ligne de base sous la pp dans les piles.
      style={{ display: "block", flex: "none" }}
    />
  );
}
