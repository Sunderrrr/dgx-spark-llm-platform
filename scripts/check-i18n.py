#!/usr/bin/env python3
"""Contrôle de couverture i18n de l'interface (à lancer depuis la racine du dépôt).

Pourquoi ce script existe
-------------------------
Le contrat i18n du portail est dans `dgx-portal-frontend/lib/i18n.tsx` : le texte
**français sert de clé**, l'anglais est un dictionnaire, et **une clé manquante
retombe silencieusement sur le français**. Ce silence est le problème : rien ne
casse, personne ne voit l'anglais manquant, et l'utilisateur anglophone lit du
français. Le même angle mort existait pour le formatage (une vingtaine de
`toLocaleString("fr-FR")` en dur affichaient « 1 234 » à un lecteur anglophone).

Ce script rend ces deux angles morts bruyants. Il vérifie :

1. toute chaîne `t("…")` littérale a une entrée dans le dictionnaire `EN` ;
2. aucune locale de formatage n'est écrite en dur hors de `lib/i18n.tsx`.

Il **rapporte sans échouer** les entrées du dictionnaire qu'il ne voit jamais
utilisées : un `t(variable)` (libellé venu du serveur, tableau d'options…) est
invisible à l'analyse statique, donc cette liste contient de faux positifs — ne
pas s'en servir pour supprimer des clés.

Sortie : 0 si tout est couvert, 1 sinon. Usage : `python3 scripts/check-i18n.py`.
"""
import os
import re
import sys

RACINE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'dgx-portal-frontend')
I18N = os.path.join(RACINE, 'lib', 'i18n.tsx')
# Seul fichier autorisé à écrire une locale en dur : c'est là qu'elle est définie.
LOCALE_AUTORISEE = {os.path.join('lib', 'i18n.tsx')}
IGNORES = ('node_modules', '.next', '.git')


def _fichiers():
    for base, dossiers, noms in os.walk(RACINE):
        dossiers[:] = [d for d in dossiers if d not in IGNORES]
        for n in noms:
            if n.endswith(('.ts', '.tsx')):
                yield os.path.join(base, n)


def _cles_en():
    """Clés déclarées dans le bloc `const EN: Record<string, string> = { … }`.

    Une clé commence une ligne et est suivie de « : ». Une valeur multi-ligne ne
    matche donc pas (elle n'est pas suivie de deux-points) — ce qui évite de
    compter les valeurs comme des clés.
    """
    src = open(I18N, encoding='utf-8').read()
    debut = src.index('const EN: Record<string, string> = {')
    fin = src.index('\n};', debut)
    bloc = src[debut:fin]
    cles = set()
    for motif in (r'^\s*"((?:[^"\\]|\\.)*)"\s*:', r"^\s*'((?:[^'\\]|\\.)*)'\s*:"):
        cles.update(m.group(1) for m in re.finditer(motif, bloc, re.M))
    return cles


def _appels_t():
    """(chaîne, fichier:ligne) pour chaque `t("…")` littéral trouvé."""
    trouves = []
    for chemin in _fichiers():
        if os.path.abspath(chemin) == os.path.abspath(I18N):
            continue
        rel = os.path.relpath(chemin, RACINE)
        txt = open(chemin, encoding='utf-8').read()
        # Le hook peut être renommé localement (`const tr = useT()`).
        alias = set(re.findall(r'const\s+(\w+)\s*=\s*useT\(\)', txt)) | {'t'}
        for a in alias:
            motif = (rf'\b{re.escape(a)}\(\s*(?:"((?:[^"\\]|\\.)*)"'
                     r"|'((?:[^'\\]|\\.)*)'|`([^`$]*)`)\s*[,)]")
            for m in re.finditer(motif, txt):
                cle = m.group(1) or m.group(2) or m.group(3)
                if cle and cle.strip():
                    ligne = txt[:m.start()].count('\n') + 1
                    trouves.append((cle, f'{rel}:{ligne}'))
    return trouves


def main():
    cles = _cles_en()
    appels = _appels_t()
    utilisees = {c for c, _ in appels}

    manquantes = [(c, ou) for c, ou in appels if c not in cles]
    locales = []
    for chemin in _fichiers():
        rel = os.path.relpath(chemin, RACINE)
        if rel in LOCALE_AUTORISEE:
            continue
        for i, ligne in enumerate(open(chemin, encoding='utf-8'), 1):
            for m in re.finditer(r'toLocale\w*\(\s*(?:\)|["\']([a-zA-Z-]+)["\'])', ligne):
                locales.append((m.group(1) or '<navigateur>', f'{rel}:{i}'))

    print(f'  {len(cles)} clés EN, {len(utilisees)} chaînes t() distinctes')
    ko = 0
    if manquantes:
        ko = 1
        print(f'  ✗ {len(manquantes)} chaîne(s) sans traduction anglaise :')
        for c, ou in manquantes:
            print(f'      {c[:90]}\n        {ou}')
    else:
        print('  ✓ toutes les chaînes t() ont leur traduction anglaise')
    if locales:
        ko = 1
        print(f'  ✗ {len(locales)} formatage(s) à locale figée (utiliser useLocale()) :')
        for loc, ou in locales:
            print(f'      {loc:<12} {ou}')
    else:
        print('  ✓ aucun formatage à locale figée')

    mortes = sorted(cles - utilisees)
    if mortes:
        print(f'  · {len(mortes)} clé(s) EN jamais vues dans un t() littéral — '
              'indicatif seulement : un t(variable) est invisible ici.')
    return ko


if __name__ == '__main__':
    sys.exit(main())
