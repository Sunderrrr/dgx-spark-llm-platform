"use client";

import { Icon } from "@astryxdesign/core/Icon";
import { VStack } from "@astryxdesign/core/Stack";
import { FilmIcon, PhotoIcon } from "@heroicons/react/24/outline";

/** Le pavé « génération en cours » des pages Image et Vidéo (balayage
 * `.video-generating` de globals.css) : occupe la place exacte du média à
 * venir pendant le rendu, au lieu d'un spinner qui laisse le saut de mise en
 * page quand le résultat arrive.
 *
 * Le même que dans ces deux pages, donc le même dans le playground quand
 * `generer_image` / `generer_video` tournent : une seule pièce à faire
 * bouger. L'ICÔNE dit quel média arrive (photo ou pellicule) — le reste est
 * la classe CSS, qui gère déjà `prefers-reduced-motion`. */
export function GenerationPlaceholder({ media }: { media: "image" | "video" }) {
  return (
    <VStack
      className="video-generating"
      height="100%"
      width="100%"
      hAlign="center"
      vAlign="center"
      gap={2}
    >
      <Icon icon={media === "image" ? PhotoIcon : FilmIcon} size="lg" color="secondary" />
    </VStack>
  );
}
