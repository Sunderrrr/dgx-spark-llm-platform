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

ROOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'dgx-portal-frontend')
I18N = os.path.join(ROOT_DIR, 'lib', 'i18n.tsx')
# Only file allowed to hardcode a locale: it is where the locale is defined.
ALLOWED_LOCALE = {os.path.join('lib', 'i18n.tsx')}
IGNORES = ('node_modules', '.next', '.git')


def _files():
    for base, folders, names in os.walk(ROOT_DIR):
        folders[:] = [d for d in folders if d not in IGNORES]
        for n in names:
            if n.endswith(('.ts', '.tsx')):
                yield os.path.join(base, n)


def _en_keys():
    """Keys declared in the `const EN: Record<string, string> = { … }` block.

    A key starts a line and is followed by « : ». A multi-line value therefore
    does not match (it is not followed by a colon) — which avoids counting
    values as keys.
    """
    src = open(I18N, encoding='utf-8').read()
    start = src.index('const EN: Record<string, string> = {')
    end = src.index('\n};', start)
    block = src[start:end]
    keys = set()
    for pattern in (r'^\s*"((?:[^"\\]|\\.)*)"\s*:', r"^\s*'((?:[^'\\]|\\.)*)'\s*:"):
        keys.update(m.group(1) for m in re.finditer(pattern, block, re.M))
    return keys


def _calls_t():
    """(string, file:line) for each literal `t("…")` found."""
    found = []
    for path_ in _files():
        if os.path.abspath(path_) == os.path.abspath(I18N):
            continue
        rel = os.path.relpath(path_, ROOT_DIR)
        txt = open(path_, encoding='utf-8').read()
        # The hook can be renamed locally (`const tr = useT()`).
        alias = set(re.findall(r'const\s+(\w+)\s*=\s*useT\(\)', txt)) | {'t'}
        for a in alias:
            pattern = (rf'\b{re.escape(a)}\(\s*(?:"((?:[^"\\]|\\.)*)"'
                     r"|'((?:[^'\\]|\\.)*)'|`([^`$]*)`)\s*[,)]")
            for m in re.finditer(pattern, txt):
                key = m.group(1) or m.group(2) or m.group(3)
                if key and key.strip():
                    line_no = txt[:m.start()].count('\n') + 1
                    found.append((key, f'{rel}:{line_no}'))
    return found


def main():
    keys = _en_keys()
    calls = _calls_t()
    used = {c for c, _ in calls}

    missing = [(c, where) for c, where in calls if c not in keys]
    locales = []
    for path_ in _files():
        rel = os.path.relpath(path_, ROOT_DIR)
        if rel in ALLOWED_LOCALE:
            continue
        for i, line in enumerate(open(path_, encoding='utf-8'), 1):
            for m in re.finditer(r'toLocale\w*\(\s*(?:\)|["\']([a-zA-Z-]+)["\'])', line):
                locales.append((m.group(1) or '<browser>', f'{rel}:{i}'))

    print(f'  {len(keys)} EN keys, {len(used)} distinct t() strings')
    ko = 0
    if missing:
        ko = 1
        print(f'  ✗ {len(missing)} string(s) without an English translation:')
        for c, where in missing:
            print(f'      {c[:90]}\n        {where}')
    else:
        print('  ✓ every t() string has its English translation')
    if locales:
        ko = 1
        print(f'  ✗ {len(locales)} hardcoded-locale formatting(s) (use useLocale()):')
        for loc, where in locales:
            print(f'      {loc:<12} {where}')
    else:
        print('  ✓ no hardcoded-locale formatting')

    unused = sorted(keys - used)
    if unused:
        print(f'  · {len(unused)} EN key(s) never seen in a literal t() — '
              'indicative only: a t(variable) is invisible here.')
    return ko


if __name__ == '__main__':
    sys.exit(main())
