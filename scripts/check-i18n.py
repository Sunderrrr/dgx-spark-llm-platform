#!/usr/bin/env python3
"""UI i18n coverage check (run from the repo root).

Why this script exists
----------------------
The portal's i18n contract lives in `dgx-portal-frontend/lib/i18n.tsx`: the
**French text serves as the key**, English is a dictionary, and **a missing key
silently falls back to French**. That silence is the problem: nothing breaks,
nobody sees the missing English, and the English-speaking user reads French.
The same blind spot existed for formatting (about twenty hardcoded
`toLocaleString("fr-FR")` displayed « 1 234 » to an English-speaking reader).

This script makes both blind spots noisy. It checks:

1. every literal `t("…")` string has an entry in the `EN` dictionary;
2. no formatting locale is hardcoded outside `lib/i18n.tsx`.

It **reports without failing** the dictionary entries it never sees used: a
`t(variable)` (label from the server, options table…) is invisible to static
analysis, so that list contains false positives — do not use it to delete keys.

Output: 0 if everything is covered, 1 otherwise. Usage: `python3 scripts/check-i18n.py`.
"""
import os
import re
import sys

RACINE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'dgx-portal-frontend')
I18N = os.path.join(RACINE, 'lib', 'i18n.tsx')
# Only file allowed to hardcode a locale: it is where the locale is defined.
LOCALE_AUTORISEE = {os.path.join('lib', 'i18n.tsx')}
IGNORES = ('node_modules', '.next', '.git')


def _fichiers():
    for base, dossiers, noms in os.walk(RACINE):
        dossiers[:] = [d for d in dossiers if d not in IGNORES]
        for n in noms:
            if n.endswith(('.ts', '.tsx')):
                yield os.path.join(base, n)


def _cles_en():
    """Keys declared in the `const EN: Record<string, string> = { … }` block.

    A key starts a line and is followed by « : ». A multi-line value therefore
    does not match (it is not followed by a colon) — which avoids counting
    values as keys.
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
    """(string, file:line) for each literal `t("…")` found."""
    trouves = []
    for chemin in _fichiers():
        if os.path.abspath(chemin) == os.path.abspath(I18N):
            continue
        rel = os.path.relpath(chemin, RACINE)
        txt = open(chemin, encoding='utf-8').read()
        # The hook can be renamed locally (`const tr = useT()`).
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
