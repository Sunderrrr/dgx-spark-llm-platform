#!/usr/bin/env python3
"""Regenerate the portal logo from the designer's source file.

The produced file is `dgx-portal-frontend/app/favicon.ico`, served by Next at
the site root. It is used in TWO places, deliberately: the browser tab icon,
and the logo at the top left of the sidebar, right before « Cronos »
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

Usage :
    scripts/generer-favicon.py [path-to-logo.png]

Without argument: ~/Images/dgx.png (the designer's file).
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

SOURCE_DEFAUT = Path.home() / "Images" / "dgx.png"
DESTINATION = Path(__file__).resolve().parent.parent / "dgx-portal-frontend" / "app" / "favicon.ico"

# Sizes embedded in the ICO: tab (16/32), taskbar (48), and enough to stay
# sharp on a dense screen (64/128, which the browser scales down).
TAILLES = (16, 32, 48, 64, 128)

# Below this alpha level, it is the halo and not the emblem.
SEUIL_ALPHA = 8


def embleme_carreen(chemin: Path) -> Image.Image:
    """Visible content of the logo, centered on a transparent square."""
    logo = Image.open(chemin).convert("RGBA")
    visible = logo.getchannel("A").point(lambda v: 255 if v > SEUIL_ALPHA else 0)
    boite = visible.getbbox()
    if boite is None:
        raise SystemExit(f"{chemin} : image entièrement transparente, rien à générer.")
    embleme = logo.crop(boite)
    cote = max(embleme.size)
    carre = Image.new("RGBA", (cote, cote), (0, 0, 0, 0))
    carre.paste(embleme, ((cote - embleme.width) // 2, (cote - embleme.height) // 2), embleme)
    return carre


def main() -> int:
    source = Path(sys.argv[1]) if len(sys.argv) > 1 else SOURCE_DEFAUT
    if not source.is_file():
        raise SystemExit(f"Logo introuvable : {source}")

    carre = embleme_carreen(source)
    grand = carre.resize((max(TAILLES), max(TAILLES)), Image.LANCZOS)
    grand.save(DESTINATION, format="ICO", sizes=[(t, t) for t in TAILLES])

    print(f"  source      : {source} ({Image.open(source).size[0]}x{Image.open(source).size[1]})")
    print(f"  emblème     : {carre.size[0]}x{carre.size[1]} → carré {max(TAILLES)}x{max(TAILLES)}")
    print(f"  écrit       : {DESTINATION.name} ({DESTINATION.stat().st_size} octets)")

    relu = Image.open(DESTINATION)
    print(f"  tailles     : {sorted(relu.ico.sizes())}")
    modes = {relu.ico.getimage(t).mode for t in relu.ico.sizes()}
    print(f"  modes       : {sorted(modes)}  (RGBA exigé par Turbopack)")
    if modes != {"RGBA"}:
        raise SystemExit("Échec : toutes les tailles doivent être en RGBA.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
