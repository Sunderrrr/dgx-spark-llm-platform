"use client";

import { Avatar, resolveSize, type AvatarSize } from "@astryxdesign/core/Avatar";
import { Blobatar } from "@blobatar/react";

/**
 * An account's avatar: the brand logo they chose, otherwise the blobatar
 * generated from their handle.
 *
 * Why [blobatar](https://github.com/Alain00/blobatar) rather than a home-made
 * drawing: determinism is a verified contract (same handle → same creature,
 * including across minor versions — the tests hold 1 312 renders and a
 * histogram of shapes over 20 000 seeds), it has no dependency, and its
 * idle animation — breathing, nodding, blinking, gazing — is made of
 * variables drawn from the handle, so two neighbours do not animate in sync.
 * It emits neither `<defs>`, nor gradient, nor filter: several hundred
 * avatars on a page cannot collide identifiers. Finally the contrast of the
 * eyes on the body is guaranteed (≥ 4,5:1) at every hue.
 *
 * The motion turns itself off under `prefers-reduced-motion` (rule in
 * `blobatar/motion.css`, imported by `app/layout.tsx`): nothing to handle
 * here, and above all not a media query in the SVG, which would not be
 * evaluated there.
 *
 * A brand logo, for its part, stays a file served by the portal: an `<img>`
 * via `<Avatar>`, so nothing to generate — and it does not animate.
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
      // Never an empty string: blobatar hashes whatever it is given, and « ? » is
      // already the monogram fallback.
      name={username || name || "?"}
      // The SVG wants pixels; `resolveSize` is the function that translates
      // the design-system scale (xsm 20, sm 24, md 36, lg 48, xl 128) — copying
      // it here would make it diverge at the first token change.
      size={resolveSize(size)}
      // Full disc: the avatar fills the circle, exactly like the Astryx `Avatar`
      // it sits next to.
      background="circle"
      // « always » and not « hover »: it is someone's avatar, it must live
      // where it is looked at — sidebar, settings, admin list, which is
      // paginated at ten rows. « hover » is the library's advice for a grid
      // of several hundreds, which this page is not.
      animate="always"
      title={name || username || ""}
      // `display: block`: an inline SVG is an « inline » box by default, and
      // would leave the baseline space under the avatar in stacks.
      style={{ display: "block", flex: "none" }}
    />
  );
}
