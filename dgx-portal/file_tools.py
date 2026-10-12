"""Playground file editing — targeted edits instead of full rewrites.

The playground used to make the model rewrite a whole file for any change.
A partial-edit protocol existed before (a ```edit block applied by the
browser) and was removed: the model copied an anchor slightly wrong, the
edit silently did not apply, and NOTHING told the model. What makes targeted
edits reliable is the feedback loop, not the format — the approach of
Claude's text editor tool:

  * `str_replace` replaces text that must appear EXACTLY ONCE in the file;
    zero or several matches is an error the model reads and fixes on its
    next round (more context, or `view_file` first);
  * `view_file` shows the CURRENT content with line numbers, so the model
    edits what is really there and not its memory of it;
  * `create_file` writes a new file, or replaces one wholesale when most of
    it changes — a rewrite that keeps most lines is refused (`_mostly_kept`):
    on a 2 000-line HTML file it costs minutes of generation.

The tools run on the server, on the files the browser sends with the
request (the latest version of each file of the conversation). The changed
files go back as `dgx_file` events: the browser records the new version
and shows a diff; the final answer only summarizes the change.
"""
import difflib
import re

# Bounds: the browser sends the conversation's files with every request
# that may edit them — they must not turn into an unbounded upload.
MAX_FILES = 40
MAX_FILE_CHARS = 400_000
MAX_TOTAL_CHARS = 1_200_000
# What one `view_file` returns, and what a tool result may weigh in the prompt.
VIEW_MAX_LINES = 400
SNIPPET_CONTEXT = 4

FILE_TOOL_NAMES = ('view_file', 'str_replace', 'create_file')

# A request to CHANGE something. Only then do we pay the extra model round
# of the tool phase; a « merci » in a conversation with files must not.
_EDIT_REQUEST = re.compile(
    r"\b(?:modif|chang|corrig|r[ée]par|ajout|rajout|remplac|supprim|enl[eè]v|retir|"
    r"renomm|d[ée]plac|mets?\b|passe\b|am[ée]lior|optimis|refactor|r[ée][ée]cri|adapt|"
    r"fix|edit|updat|add|remov|delet|renam|replac|improv|rewrit|tweak|mov)"
    # Follow-up corrections carry no verb: « en fait pas traefik mais nginx
    # proxy manager », « plutôt en Python », « actually use port 8080 ».
    r"|\b(?:en fai(?:t|te)|plut[oô]t|au lieu|[àa] la place|pas \S+(?: \S+){0,3} mais|"
    r"int[ée]gr|inclu|utilis|bascul|transform|convert|"
    r"actually|instead|rather|switch|use\b|make it|turn it)",
    re.I)


def parse_files(raw):
    """{path: content} from the request's `files`, bounded and sanitized."""
    files, total = {}, 0
    if not isinstance(raw, list):
        return files
    for f in raw[:MAX_FILES]:
        if not isinstance(f, dict):
            continue
        name = str(f.get('name') or '').strip()[:200]
        content = f.get('content')
        if not name or not isinstance(content, str) or len(content) > MAX_FILE_CHARS:
            continue
        if total + len(content) > MAX_TOTAL_CHARS:
            break
        total += len(content)
        files[name] = content
    return files


# A clarifying questionnaire of the model (```ask block or {"questions": [...]}).
_ASKED = re.compile(r'```ask\b|"questions"\s*:\s*\[')


def edit_requested(history):
    """Does the last user message ask to change something?

    When the model answered the request with clarifying questions, the last
    user message is only the ANSWERS (« npm », « oui ») — the change itself is
    in the user message before the questionnaire, so that one counts too."""
    turns = [m for m in history if m.get('role') in ('user', 'assistant')]
    asks = lambda m: bool(_EDIT_REQUEST.search(str(m.get('content', ''))[:4000]))
    if not turns or turns[-1].get('role') != 'user':
        return False
    if asks(turns[-1]):
        return True
    return (len(turns) >= 3
            and turns[-2].get('role') == 'assistant'
            and bool(_ASKED.search(str(turns[-2].get('content', ''))))
            and turns[-3].get('role') == 'user' and asks(turns[-3]))


def _file_tools():
    return [
        {'type': 'function', 'function': {
            'name': 'view_file',
            'description': (
                "Show the CURRENT content of a file of this conversation, with "
                "line numbers. Call it before editing when you are not sure of "
                "the exact text: an edit must copy the file exactly."),
            'parameters': {'type': 'object', 'properties': {
                'path': {'type': 'string', 'description': "File name, as listed."},
                'start_line': {'type': 'integer', 'description': "First line (1-based, optional)."},
                'end_line': {'type': 'integer', 'description': "Last line (optional)."}},
                'required': ['path']}}},
        {'type': 'function', 'function': {
            'name': 'str_replace',
            'description': (
                "Edit a file by replacing ONE exact passage. `old_str` must "
                "match the file character for character (indentation "
                "included) and appear exactly once — add surrounding lines "
                "to make it unique. Prefer this to rewriting the file. Several "
                "changes = several calls."),
            'parameters': {'type': 'object', 'properties': {
                'path': {'type': 'string'},
                'old_str': {'type': 'string', 'description': "Exact text to replace."},
                'new_str': {'type': 'string', 'description': "Replacement text (may be empty)."}},
                'required': ['path', 'old_str', 'new_str']}}},
        {'type': 'function', 'function': {
            'name': 'create_file',
            'description': (
                "Create a new file, or replace a file entirely when most of it "
                "changes. For a localized change use str_replace."),
            'parameters': {'type': 'object', 'properties': {
                'path': {'type': 'string'},
                'content': {'type': 'string', 'description': "The COMPLETE file."}},
                'required': ['path', 'content']}}},
    ]


def files_note(files):
    """System note listing the files, for the tool decision."""
    listing = ", ".join(f"{name} ({content.count(chr(10)) + 1} lines)"
                        for name, content in files.items())
    return ("Files of this conversation (current versions): " + listing + ". "
            "The user is asking for a change: APPLY it now with the file tools — "
            "str_replace (exact, unique old_str) for each modification, view_file "
            "first if you need the current text, create_file only for a NEW file. "
            "This overrides any instruction to output a file complete: never "
            "rewrite a whole file for a localized change. When every change is "
            "applied, answer with a short summary.")


def _resolve(files, path):
    """The file a path designates: exact name, else a suffix match (the model
    writes « index.html » for « site/index.html »)."""
    path = str(path or '').strip().strip('`')
    if path in files:
        return path
    for name in files:
        if name.endswith('/' + path) or path.endswith('/' + name):
            return name
    return None


def _numbered(text, start=1):
    return "\n".join(f"{start + i:6}\t{line}" for i, line in enumerate(text.split("\n")))


def _line_of(content, index):
    return content.count("\n", 0, index) + 1


def announce(name, args):
    """The step shown while a file tool runs."""
    return {'step': 'file_edit' if name != 'view_file' else 'file_view',
            'tool': 'files', 'question': str(args.get('path') or '')[:200]}


def exec_file_tool(name, args, files, changes):
    """Runs a file tool on `files` (modified in place). Returns (result text
    for the model, error or None). `changes` records {path: original} for
    every file touched, so the caller can send each one back once."""
    if not isinstance(args, dict):
        args = {}
    if name == 'create_file':
        path = str(args.get('path') or '').strip().strip('`')[:200]
        content = args.get('content')
        if not path or not isinstance(content, str):
            return "Error: create_file needs `path` and `content`.", "invalid arguments"
        if len(content) > MAX_FILE_CHARS:
            return "Error: file too large.", "file too large"
        target = _resolve(files, path) or path
        if target in files and _mostly_kept(files[target], content):
            return (f"Error: {target} already exists and this rewrite keeps most "
                    "of it unchanged. Use str_replace for each change instead "
                    "(several calls in one round are fine). No change made.",
                    "rewrite refused")
        changes.setdefault(target, files.get(target))
        files[target] = content
        return f"File {target} written ({content.count(chr(10)) + 1} lines).", None

    path = _resolve(files, args.get('path'))
    if path is None:
        known = ", ".join(files) or "none"
        return (f"Error: no file named {args.get('path')!r}. Files: {known}.",
                "unknown file")
    content = files[path]

    if name == 'view_file':
        lines = content.split("\n")
        try:
            start = max(1, int(args.get('start_line') or 1))
        except (TypeError, ValueError):
            start = 1
        try:
            end = int(args.get('end_line') or 0)
        except (TypeError, ValueError):
            end = 0
        end = min(len(lines), end if end >= start else start + VIEW_MAX_LINES - 1,
                  start + VIEW_MAX_LINES - 1)
        body = _numbered("\n".join(lines[start - 1:end]), start)
        more = (f"\n[lines {start}-{end} of {len(lines)}; call again with "
                f"start_line={end + 1} for the rest]" if end < len(lines) else "")
        return f"{path}:\n{body}{more}", None

    if name == 'str_replace':
        old, new = args.get('old_str'), args.get('new_str', '')
        if not isinstance(old, str) or not old:
            return "Error: `old_str` must be a non-empty string.", "invalid arguments"
        if not isinstance(new, str):
            new = str(new)
        count = content.count(old)
        if count == 0:
            hint = ""
            # The most frequent miss: indentation or trailing spaces.
            stripped = "\n".join(l.strip() for l in old.split("\n"))
            if stripped and stripped in "\n".join(l.strip() for l in content.split("\n")):
                hint = (" The text exists with DIFFERENT indentation or spacing: "
                        "call view_file and copy it exactly.")
            return (f"Error: old_str not found in {path}.{hint} No change made.",
                    "text not found")
        if count > 1:
            where, pos = [], content.find(old)
            while pos != -1 and len(where) < 8:
                where.append(str(_line_of(content, pos)))
                pos = content.find(old, pos + 1)
            return (f"Error: old_str appears {count} times in {path} (lines "
                    f"{', '.join(where)}). Include more surrounding lines so it "
                    "is unique. No change made.", "ambiguous text")
        pos = content.find(old)
        changes.setdefault(path, content)
        updated = content[:pos] + new + content[pos + len(old):]
        files[path] = updated
        first = _line_of(updated, pos)
        last = first + new.count("\n")
        lines = updated.split("\n")
        lo, hi = max(1, first - SNIPPET_CONTEXT), min(len(lines), last + SNIPPET_CONTEXT)
        return (f"Edited {path}. Snippet of the result:\n"
                f"{_numbered(chr(10).join(lines[lo - 1:hi]), lo)}"), None

    return f"Error: unknown tool {name}.", "unknown tool"


# A rewrite keeping at least this share of the old lines is a localized
# change in disguise (2026-10-12: « en fait c'est avec traefik » rewrote the
# whole compose through create_file).
REWRITE_KEPT_MAX = 0.5


def _mostly_kept(old, new):
    old_lines = old.split("\n")
    if len(old_lines) < 20:
        return False       # small files: rewriting costs nothing
    sm = difflib.SequenceMatcher(None, old_lines, new.split("\n"), autojunk=False)
    kept = sum(b.size for b in sm.get_matching_blocks())
    return kept / len(old_lines) >= REWRITE_KEPT_MAX


def edits_note(files, changes):
    """Text appended to the last user message once edits were applied: the
    final answer must not reprint the files."""
    touched = [p for p in changes if files.get(p) != changes[p]]
    if not touched:
        return ''
    return ("\n\n---\n[The platform ALREADY applied your edits to: "
            + ", ".join(touched) + ". The user sees the updated files and a "
            "diff. Do NOT output these files again; briefly explain what you "
            "changed, in the user's language.]")
