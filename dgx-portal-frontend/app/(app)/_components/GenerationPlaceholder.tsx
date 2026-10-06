"use client";

import { Icon } from "@astryxdesign/core/Icon";
import { VStack } from "@astryxdesign/core/Stack";
import { FilmIcon, PhotoIcon } from "@heroicons/react/24/outline";

/** The « génération en cours » tile of the Image and Video pages (the
 * `.video-generating` sweep of globals.css): it takes the exact place of
 * the media to come during rendering, instead of a spinner that leaves the
 * layout jump when the result arrives.
 *
 * The same one as in those two pages, hence the same one in the playground
 * when `generer_image` / `generer_video` run: a single piece to keep
 * moving. The ICON says which media is coming (photo or film strip) — the
 * rest is the CSS class, which already handles `prefers-reduced-motion`. */
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
