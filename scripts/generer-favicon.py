#!/usr/bin/env python3
"""Régénère le logo du portail depuis le fichier source du designer.

Le fichier produit est `dgx-portal-frontend/app/favicon.ico`, servi par Next à
la racine du site. Il sert à DEUX endroits, volontairement : l'icône de l'onglet
du navigateur, et le logo en haut à gauche de la barre latérale, juste avant
« Cronos » (`app/(app)/layout.tsx`, `SideNavHeading` → `icon`). Une seule source
pour les deux, donc : remplacer le logo met les deux à jour d'un coup.

Deux contraintes ont dicté le traitement :

* **Un favicon doit être carré.** Le logo source est un emblème plus large que
  haut, posé sur fond transparent. On recadre sur le contenu VISIBLE (seuil
  d'alpha : le pourtour porte un halo presque transparent qui, pris en compte,
  décentrerait l'emblème vers le bas), puis on centre ce contenu sur un carré
  transparent. Aucun étirement : les proportions du designer sont conservées.

* **Turbopack refuse un ICO dont les PNG embarqués ne sont pas en RGBA** et
  échoue sur « The PNG is not in RGBA format! ». L'image est donc convertie en
  RGBA AVANT l'écriture — c'est ce qui fait passer `next build`.

Le carré est calculé sur la plus grande taille demandée, puis réduit pour
chaque taille : un seul rééchantillonnage par taille, pas de dégradation en
cascade.

Usage :
    scripts/generer-favicon.py [chemin-du-logo.png]

Sans argument : /home/mael/Images/dgx.png (le fichier du designer).
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

SOURCE_DEFAUT = Path("/home/mael/Images/dgx.png")
DESTINATION = Path(__file__).resolve().parent.parent / "dgx-portal-frontend" / "app" / "favicon.ico"

# Tailles embarquées dans l'ICO : onglet (16/32), barre des tâches (48), et de
# quoi rester net sur un écran dense (64/128, que le navigateur réduit).
TAILLES = (16, 32, 48, 64, 128)

# En dessous de ce niveau d'alpha, c'est le halo et non l'emblème.
SEUIL_ALPHA = 8


def embleme_carreen(chemin: Path) -> Image.Image:
    """Contenu visible du logo, centré sur un carré transparent."""
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
