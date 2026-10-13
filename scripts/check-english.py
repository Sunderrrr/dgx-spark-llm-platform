#!/usr/bin/env python3
"""The code speaks English: comments, docstrings, log lines and identifiers.

Operator rule: « tout le code en anglais, l'interface en français ET en
anglais ». What may stay French is what the USER reads:
  - the UI msgids (`t("…")`, lib/i18n.tsx, lib/notices.ts) — French is the
    msgid language and English lives in the dictionary;
  - server messages returned to the browser (`jsonify({'error': "…"})`,
    flash texts, SSE notices) — French msgids translated at display time by
    `tServer`;
  - prompts and prompt fragments the model reads in the user's language;
  - test fixtures (they reproduce what users type).

So this gate only reads COMMENTS (Python `#` and docstrings, TS `//` and
`/* */`, shell `#`) and LOG CALLS (`_log.*`, `logger.*`, `print(`,
`console.*`), plus Python/TS identifiers. A line is French when, once the
quoted parts are removed (« … », "…", '…', `…` quote UI text on purpose),
it still holds several French function words — a heuristic tuned so that an
English comment quoting a French message is not flagged.

Usage:
  python3 scripts/check-english.py            # fail on any French line
  python3 scripts/check-english.py --report   # list them, exit 0
"""
import io
import re
import subprocess
import sys
import tokenize

# French function words that English prose never uses. Short ambiguous ones
# (« a », « on », « pour » in names…) are left out on purpose.
FRENCH_WORDS = {
    'le', 'les', 'des', 'une', 'du', 'est', 'sont', 'pour', 'avec', 'dans',
    'qui', 'que', 'pas', 'sur', 'cette', 'ces', 'mais', 'aussi', 'donc',
    'quand', 'sans', 'leur', 'tout', 'tous', 'fait', 'être', 'avoir', 'ont',
    'nous', 'vous', 'il', 'elle', 'ils', 'déjà', 'très', 'peut', 'doit',
    'ici', 'comme', 'plus', 'même', 'où', 'si', 'au', 'aux', 'ne', 'se',
    'son', 'sa', 'ses', 'cela', 'ça', 'car', 'puis', 'alors', 'chaque',
    'lorsque', 'après', 'avant', 'entre', 'selon', 'sinon', 'encore',
}
# Words with French accents that are common in French prose.
ACCENT_RE = re.compile(r"\b\w*[éèêàùçôîû]\w*\b", re.I)
QUOTED_RE = re.compile(r"«[^»]*»|\"[^\"\n]*\"|'[^'\n]*'|`[^`\n]*`|“[^”]*”")
WORD_RE = re.compile(r"[A-Za-zÀ-ÿ']+")

# Files whose French is the product (UI text, prompts, fixtures) or vendored.
EXEMPT = re.compile(
    r"(^|/)(lib/i18n\.tsx|lib/notices\.ts|tests?/|node_modules/|\.next/"
    r"|CHANGELOG\.md|docs/|README|.*\.md$|.*\.json$|.*\.css$|\.github/ISSUE_TEMPLATE/"
    r"|scripts/check-english\.py$)"
)
CODE_EXT = ('.py', '.ts', '.tsx', '.js', '.mjs', '.sh')
# Configuration whose `#` comments are read like code comments.
CONFIG_RE = re.compile(r"(Dockerfile[^/]*|\.ya?ml|\.service|\.timer|\.path|\.conf|\.toml"
                       r"|requirements[^/]*\.txt|\.env\.example|(^|/)sudoers\.d-[^/]*)$")


def french(text):
    """True when `text` reads as French prose (quotes removed first)."""
    bare = QUOTED_RE.sub(' ', text)
    words = [w.lower().strip("'") for w in WORD_RE.findall(bare)]
    if len(words) < 3:
        return False
    hits = sum(1 for w in words if w in FRENCH_WORDS)
    accents = len(ACCENT_RE.findall(bare))
    return hits >= 2 and (hits + accents) / len(words) >= 0.2 or hits >= 4


def unquote_across_lines(comments):
    """Drop « … » quotes that span several comment lines (the operator's own
    words, quoted verbatim on purpose) before the per-line check."""
    out, open_quote = [], False
    for n, text in comments:
        if open_quote:
            if '»' in text:
                text, open_quote = text.split('»', 1)[1], False
            else:
                continue
        last_open, last_close = text.rfind('«'), text.rfind('»')
        if last_open > last_close:
            text, open_quote = text[:last_open], True
        out.append((n, text))
    return out


def python_comments(path, src):
    """(line, text) of every comment and docstring of a Python file."""
    out = []
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, SyntaxError):
        return out
    prev = None
    for tok in toks:
        if tok.type == tokenize.COMMENT:
            out.append((tok.start[0], tok.string.lstrip('#')))
        elif tok.type == tokenize.STRING and prev in (None, tokenize.INDENT, tokenize.NEWLINE,
                                                       tokenize.NL, tokenize.DEDENT):
            body = tok.string
            if body.lstrip('rbuRBU').startswith(('"""', "'''")):
                for i, line in enumerate(body.splitlines()):
                    out.append((tok.start[0] + i, line))
        if tok.type not in (tokenize.COMMENT, tokenize.NL):
            prev = tok.type
    return out


TS_LINE_COMMENT = re.compile(r"(?<![:\"'`\\])//(.*)$")


def ts_comments(src):
    out, in_block = [], False
    for n, line in enumerate(src.splitlines(), 1):
        if in_block:
            out.append((n, line))
            if '*/' in line:
                in_block = False
            continue
        if '/*' in line and '*/' not in line.split('/*', 1)[1]:
            in_block = True
            out.append((n, line.split('/*', 1)[1]))
            continue
        if '/*' in line:
            out.append((n, line.split('/*', 1)[1].split('*/', 1)[0]))
        m = TS_LINE_COMMENT.search(line)
        if m:
            out.append((n, m.group(1)))
    return out


def shell_comments(src):
    return [(n, line.split('#', 1)[1]) for n, line in enumerate(src.splitlines(), 1)
            if re.match(r"\s*#(?!!)", line)]


LOG_CALL = re.compile(r"(?:_log|log|logger|logging)\.(?:debug|info|warning|error|exception|critical)\(|"
                      r"\bprint\(|console\.(?:log|warn|error|info)\(")


def log_lines(src):
    return [(n, line) for n, line in enumerate(src.splitlines(), 1) if LOG_CALL.search(line)]


def log_french(line):
    """A log line is French when its MESSAGE is: here the quotes are the text."""
    msgs = re.findall(r"[fr]?\"([^\"]*)\"|[fr]?'([^']*)'", line)
    text = ' '.join(a or b for a, b in msgs)
    words = [w.lower() for w in WORD_RE.findall(text)]
    hits = sum(1 for w in words if w in FRENCH_WORDS)
    return hits >= 2 or (hits >= 1 and ACCENT_RE.search(text) is not None)


# Python/TS identifiers made of French words (def/function/const/class names).
IDENT_RE = re.compile(r"\b(?:def|class|function|const|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)")
FRENCH_IDENT_PARTS = {
    'avant', 'apres', 'demarrage', 'arret', 'utilisateur', 'utilisateurs', 'compte',
    'fichier', 'fichiers', 'liste', 'nombre', 'chemin', 'nom', 'mot', 'passe',
    'texte', 'reponse', 'requete', 'erreur', 'verifie', 'verifier', 'efface',
    'charger', 'chargement', 'lancement', 'relance', 'demande', 'suivi', 'veilleur',
    'moteur', 'modele', 'sauvegarde', 'nettoyage', 'recolle', 'recoller', 'garde',
    'libre', 'memoire', 'tache', 'taches', 'courant', 'dernier', 'derniere',
    'premier', 'premiere', 'motif', 'raison', 'serveur', 'si', 'besoin', 'horodatage',
    'incertain', 'depuis', 'raisonnablement', 'reinitialise', 'ouverture', 'integralite',
}


def ident_french(name):
    parts = [p for p in re.split(r"_|(?<=[a-z])(?=[A-Z])", name) if p]
    lowered = [p.lower() for p in parts]
    return sum(1 for p in lowered if p in FRENCH_IDENT_PARTS) >= 1 and len(parts) >= 1 \
        and any(p in FRENCH_IDENT_PARTS - {'si', 'nom', 'mot', 'garde', 'libre', 'liste'}
                for p in lowered)


def main():
    report = '--report' in sys.argv
    files = subprocess.run(['git', 'ls-files'], capture_output=True, text=True,
                           check=True).stdout.split()
    findings = []
    for path in files:
        is_config = bool(CONFIG_RE.search(path))
        if not (path.endswith(CODE_EXT) or is_config) or EXEMPT.search(path):
            continue
        try:
            src = open(path, encoding='utf-8').read()
        except (OSError, UnicodeDecodeError):
            continue
        if path.endswith('.py'):
            comments = python_comments(path, src)
        elif path.endswith('.sh') or is_config:
            comments = shell_comments(src)
        else:
            comments = ts_comments(src)
        for n, text in unquote_across_lines(comments):
            if french(text):
                findings.append((path, n, 'comment', text.strip()))
        if is_config:
            continue
        for n, line in log_lines(src):
            if log_french(line):
                findings.append((path, n, 'log', line.strip()))
        for n, line in enumerate(src.splitlines(), 1):
            for name in IDENT_RE.findall(line):
                if ident_french(name):
                    findings.append((path, n, 'identifier', name))
    for path, n, kind, text in findings:
        print(f"{path}:{n}: {kind}: {text[:110]}")
    by_kind = {}
    for f in findings:
        by_kind[f[2]] = by_kind.get(f[2], 0) + 1
    summary = ', '.join(f"{v} {k}(s)" for k, v in sorted(by_kind.items())) or 'none'
    if findings:
        print(f"✗ French in code: {summary}")
        return 0 if report else 1
    print("✓ code is English (comments, docstrings, logs, identifiers)")
    return 0


if __name__ == '__main__':
    sys.exit(main())
