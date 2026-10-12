#!/usr/bin/env python3
"""Regenerate the portal logo from the designer's source file.

The produced file is `dgx-portal-frontend/app/favicon.ico`, served by Next at
the site root. It is used in TWO places, deliberately: the browser tab icon,
and the logo at the top left of the sidebar, right before the instance name
(`app/(app)/layout.tsx`, `SideNavHeading` → `icon`). One source for both: so
replacing the logo updates both at once.

Two constraints dictated the processing:

* **A favicon must be square.** The source logo is an emblem wider than tall,
  on a transparent background. We crop to the VISIBLE content (alpha threshold:
  the outline carries an almost transparent halo that, if counted, would push
  the emblem downwards), then center that content on a transparent square. No
  stretching: the designer's proportions are preserved.

* **Turbopack refuses an ICO whose embedded PNGs are not RGBA** and fails on
  « The PNG is not in RGBA format! ». The image is therefore converted to RGBA
  BEFORE writing — that is what makes `next build` pass.

The square is computed from the largest requested size, then reduced for each
size: a single resampling per size, no cascading degradation.

Usage:
    scripts/generate-favicon.py [path-to-logo.png]

Without argument: ~/Images/dgx.png (the designer's file).
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

DEFAULT_SOURCE = Path.home() / "Images" / "dgx.png"
DESTINATION = Path(__file__).resolve().parent.parent / "dgx-portal-frontend" / "app" / "favicon.ico"

# Sizes embedded in the ICO: tab (16/32), taskbar (48), and enough to stay
# sharp on a dense screen (64/128, which the browser scales down).
SIZES = (16, 32, 48, 64, 128)

# Below this alpha level, it is the halo and not the emblem.
ALPHA_THRESHOLD = 8


def squared_emblem(path_: Path) -> Image.Image:
    """Visible content of the logo, centered on a transparent square."""
    logo = Image.open(path_).convert("RGBA")
    visible = logo.getchannel("A").point(lambda v: 255 if v > ALPHA_THRESHOLD else 0)
    bbox = visible.getbbox()
    if bbox is None:
        raise SystemExit(f"{path_}: fully transparent image, nothing to generate.")
    emblem = logo.crop(bbox)
    side = max(emblem.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(emblem, ((side - emblem.width) // 2, (side - emblem.height) // 2), emblem)
    return square


def main() -> int:
    source = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SOURCE
    if not source.is_file():
        raise SystemExit(f"Logo not found: {source}")

    square = squared_emblem(source)
    big = square.resize((max(SIZES), max(SIZES)), Image.LANCZOS)
    big.save(DESTINATION, format="ICO", sizes=[(t, t) for t in SIZES])

    print(f"  source      : {source} ({Image.open(source).size[0]}x{Image.open(source).size[1]})")
    print(f"  emblem      : {square.size[0]}x{square.size[1]} → square {max(SIZES)}x{max(SIZES)}")
    print(f"  written     : {DESTINATION.name} ({DESTINATION.stat().st_size} bytes)")

    reread = Image.open(DESTINATION)
    print(f"  sizes       : {sorted(reread.ico.sizes())}")
    modes = {reread.ico.getimage(t).mode for t in reread.ico.sizes()}
    print(f"  modes       : {sorted(modes)}  (RGBA required by Turbopack)")
    if modes != {"RGBA"}:
        raise SystemExit("Failed: every size must be RGBA.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
