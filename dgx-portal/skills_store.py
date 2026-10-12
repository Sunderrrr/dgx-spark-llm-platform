"""Skills as Claude Code defines them: a folder with a SKILL.md and its files.

A skill is a directory whose SKILL.md starts with a YAML front matter
(`name`, `description`) followed by the instructions; the other files of the
directory (scripts, references, templates) belong to it and are read on
demand. The model first sees only `name: description`; it loads the
instructions when the skill applies, then the files it needs (progressive
disclosure — the instructions never weigh on a turn that does not use them).

Skills come from three places: written in the interface, imported from a
.zip (a skill folder, or a plugin / repository holding several), or imported
from a Git repository (GitHub, GitLab, Gitea/Codeberg, or a direct .zip URL).
A Git skill remembers its source and can be re-synchronized.

Everything here treats the archive as HOSTILE: bounded entry count and
sizes (zip bomb), no absolute path or `..` (zip slip), no symlink, text
files only — and the download goes through the platform's SSRF guard
(public https host, validated IP pinned, no redirect).
"""
import io
import json
import posixpath
import re
import zipfile
from datetime import datetime
from urllib.parse import quote, urlparse

import requests

from mcp_client import _PinnedIPHTTPSAdapter, resolve_validated_mcp_ip

MAX_ARCHIVE_BYTES = 25 * 1024 * 1024
MAX_ENTRIES = 3000
MAX_UNCOMPRESSED = 60 * 1024 * 1024
MAX_FILES_PER_SKILL = 200
MAX_FILE_BYTES = 1024 * 1024
MAX_SKILL_BYTES = 5 * 1024 * 1024
MAX_SKILLS_PER_IMPORT = 50
NAME_MAX = 64
DESCRIPTION_MAX = 1024
INSTRUCTIONS_MAX = 100_000
DOWNLOAD_TIMEOUT = 30

_NAME_RE = re.compile(r'[^a-z0-9-]+')


class SkillImportError(Exception):
    """A refusal to show as is (French, like every server message)."""


# ── SKILL.md ──────────────────────────────────────────────────────────────

def parse_front_matter(text):
    """(meta, body) of a SKILL.md. A small YAML subset — `key: value`,
    quoted values, and `>`/`|` blocks — is all SKILL.md files use; a full
    YAML parser would be one more dependency for nothing."""
    meta = {}
    text = text.lstrip('﻿')
    if not text.startswith('---'):
        return meta, text
    lines = text.split('\n')
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == '---'), None)
    if end is None:
        return meta, text
    key, block = None, []

    def _flush():
        if key is None or not block:
            return
        if meta.get(key) in ('>', '|', '>-', '|-', '>+', '|+'):
            sep = '\n' if meta[key].startswith('|') else ' '
            meta[key] = sep.join(l.strip() for l in block).strip()
        else:
            # A plain value folded over several indented lines.
            meta[key] = ' '.join([meta[key]] + [l.strip() for l in block]).strip()

    for line in lines[1:end]:
        if line[:1] in (' ', '\t') and key is not None:
            block.append(line)
            continue
        _flush()
        m = re.match(r'^([A-Za-z_][\w-]*)\s*:\s*(.*)$', line)
        if not m:
            key, block = None, []
            continue
        key, value = m.group(1), m.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
            value = value[1:-1]
        meta[key], block = value, []
    _flush()
    return meta, '\n'.join(lines[end + 1:]).lstrip('\n')


def normalize_name(name):
    """Claude's rule: lowercase letters, digits and hyphens, 64 max."""
    name = _NAME_RE.sub('-', str(name or '').strip().lower()).strip('-')
    return re.sub(r'-{2,}', '-', name)[:NAME_MAX]


def skill_from_files(files, folder_name=''):
    """{path: text} of ONE skill folder (SKILL.md at its root) → skill dict."""
    skill_md = next((p for p in files if p.lower() == 'skill.md'), None)
    if skill_md is None:
        raise SkillImportError("SKILL.md manquant.")
    meta, body = parse_front_matter(files[skill_md])
    name = normalize_name(meta.get('name') or folder_name)
    description = str(meta.get('description') or '').strip()[:DESCRIPTION_MAX]
    if not name:
        raise SkillImportError("Nom de compétence manquant (champ « name » du SKILL.md).")
    if not description:
        raise SkillImportError(f"« {name} » : description manquante (champ « description »).")
    if not body.strip():
        raise SkillImportError(f"« {name} » : SKILL.md sans instructions.")
    return {'name': name, 'description': description,
            'instructions': body.strip()[:INSTRUCTIONS_MAX],
            'version': str(meta.get('version') or '').strip()[:40],
            'files': files}


# ── Archives ──────────────────────────────────────────────────────────────

def _safe_path(name):
    """A zip member path, or None when it must not be extracted."""
    if not name or name.endswith('/') or '\x00' in name or '\\' in name:
        return None
    if name.startswith('/') or re.match(r'^[A-Za-z]:', name):
        return None
    norm = posixpath.normpath(name)
    if norm.startswith('..') or '/../' in f'/{norm}/':
        return None
    parts = norm.split('/')
    if any(p in ('.git', '__MACOSX', 'node_modules') for p in parts):
        return None
    return norm


def _decode(data):
    """UTF-8 text, or None for a binary file (skipped)."""
    if b'\x00' in data[:8192]:
        return None
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError:
        return None


def skills_from_zip(data, subpath=''):
    """Every skill found in a .zip: (skills, skipped). `skipped` lists
    [(name, reason)] for a folder refused, never silently dropped."""
    if len(data) > MAX_ARCHIVE_BYTES:
        raise SkillImportError("Archive trop volumineuse (25 Mo maximum).")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise SkillImportError("Ce fichier n'est pas une archive .zip valide.")
    infos = zf.infolist()
    if len(infos) > MAX_ENTRIES:
        raise SkillImportError("Archive trop volumineuse (trop de fichiers).")
    if sum(i.file_size for i in infos) > MAX_UNCOMPRESSED:
        raise SkillImportError("Archive trop volumineuse une fois décompressée.")
    members = {}
    for info in infos:
        # A symlink in a zip is a member whose external attributes carry
        # S_IFLNK: its « content » is a target path, never followed.
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            continue
        path = _safe_path(info.filename)
        if path:
            members[path] = info
    # GitHub/GitLab archives wrap everything in « repo-ref/ »: drop a single
    # common top-level directory.
    tops = {p.split('/')[0] for p in members}
    if len(tops) == 1 and all('/' in p for p in members):
        prefix = tops.pop() + '/'
        members = {p[len(prefix):]: i for p, i in members.items()}
    subpath = (subpath or '').strip('/')
    if subpath:
        members = {p[len(subpath) + 1:]: i for p, i in members.items()
                   if p.startswith(subpath + '/')}
        if not members:
            raise SkillImportError(f"Dossier « {subpath} » introuvable dans le dépôt.")
    roots = sorted({posixpath.dirname(p) for p in members
                    if posixpath.basename(p).lower() == 'skill.md'})
    # A skill inside another skill's folder belongs to it as a file.
    roots = [r for r in roots if not any(r != o and r.startswith(o + '/') for o in roots if o)]
    if not roots:
        raise SkillImportError("Aucun SKILL.md trouvé dans l'archive.")
    skills, skipped = [], []
    for root in roots[:MAX_SKILLS_PER_IMPORT]:
        folder = posixpath.basename(root) or 'skill'
        files, total = {}, 0
        for path, info in members.items():
            if root and not path.startswith(root + '/'):
                continue
            rel = path[len(root) + 1:] if root else path
            if info.file_size > MAX_FILE_BYTES:
                continue
            text = _decode(zf.read(info))
            if text is None:
                continue
            total += len(text.encode('utf-8'))
            if total > MAX_SKILL_BYTES or len(files) >= MAX_FILES_PER_SKILL:
                break
            files[rel] = text
        try:
            skills.append(skill_from_files(files, folder))
        except SkillImportError as e:
            skipped.append((folder, str(e)))
    for root in roots[MAX_SKILLS_PER_IMPORT:]:
        skipped.append((posixpath.basename(root), "Trop de compétences dans une seule archive."))
    return skills, skipped


def build_zip(skill_name, files):
    """A .zip of one skill, laid out like Claude Code expects it."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for path, content in sorted(files.items()):
            zf.writestr(f"{skill_name}/{path}", content)
    return buf.getvalue()


# ── Git sources ───────────────────────────────────────────────────────────

def parse_git_url(url, ref='', path=''):
    """A repository or folder URL → {'archives': [(url, ref)…], 'ref', 'path',
    'canonical'}. Several archive URLs are tried in order when the branch is
    unknown (main, then master)."""
    url = (url or '').strip()
    p = urlparse(url)
    if p.scheme != 'https' or not p.hostname:
        raise SkillImportError("L'adresse doit être un dépôt en https://.")
    host = p.hostname.lower()
    segs = [s for s in p.path.split('/') if s]
    if url.lower().endswith('.zip'):
        return {'archives': [(url, ref)], 'ref': ref, 'path': path, 'canonical': url, 'host': host}
    if len(segs) < 2:
        raise SkillImportError("Adresse de dépôt incomplète (attendu : hôte/propriétaire/dépôt).")

    def _refs(found_ref):
        r = (ref or found_ref or '').strip()
        return [r] if r else ['main', 'master']

    if host == 'github.com':
        owner, repo = segs[0], segs[1].removesuffix('.git')
        found_ref, found_path = '', ''
        if len(segs) >= 4 and segs[2] in ('tree', 'blob'):
            found_ref, found_path = segs[3], '/'.join(segs[4:])
        refs = _refs(found_ref)
        return {'archives': [(f"https://codeload.github.com/{owner}/{repo}/zip/{quote(r)}", r)
                             for r in refs],
                'ref': refs[0] if len(refs) == 1 else '', 'path': path or found_path,
                'canonical': f"https://github.com/{owner}/{repo}", 'host': host,
                'github': (owner, repo)}
    if '-' in segs:
        # GitLab: group/sub/repo/-/tree/ref/path
        k = segs.index('-')
        project = segs[:k]
        found_ref = segs[k + 2] if len(segs) > k + 2 and segs[k + 1] in ('tree', 'blob') else ''
        found_path = '/'.join(segs[k + 3:]) if found_ref else ''
    else:
        project, found_ref, found_path = segs, '', ''
        if len(segs) >= 5 and segs[2] == 'src' and segs[3] in ('branch', 'tag', 'commit'):
            # Gitea / Codeberg: owner/repo/src/branch/ref/path
            project, found_ref, found_path = segs[:2], segs[4], '/'.join(segs[5:])
    project = [s.removesuffix('.git') for s in project]
    refs = _refs(found_ref)
    if 'gitlab' in host:
        name = project[-1]
        archives = [(f"https://{host}/{'/'.join(project)}/-/archive/{quote(r)}/{name}-{quote(r)}.zip", r)
                    for r in refs]
    else:
        archives = [(f"https://{host}/{'/'.join(project[:2])}/archive/{quote(r)}.zip", r) for r in refs]
    return {'archives': archives, 'ref': refs[0] if len(refs) == 1 else '',
            'path': path or found_path, 'canonical': f"https://{host}/{'/'.join(project)}",
            'host': host}


def _safe_get(url, accept='application/zip'):
    """GET through the SSRF guard: https, public validated IP pinned, no
    redirect, bounded body. Returns (status, bytes)."""
    ok, err, ip = resolve_validated_mcp_ip(url)
    if not ok:
        raise SkillImportError(err)
    session = requests.Session()
    session.mount('https://', _PinnedIPHTTPSAdapter(ip))
    try:
        r = session.get(url, headers={'Accept': accept, 'User-Agent': 'dgx-skills-import'},
                        timeout=DOWNLOAD_TIMEOUT, allow_redirects=False, stream=True)
        try:
            if r.status_code != 200:
                return r.status_code, b''
            chunks, size = [], 0
            for chunk in r.iter_content(65536):
                size += len(chunk)
                if size > MAX_ARCHIVE_BYTES:
                    raise SkillImportError("Archive trop volumineuse (25 Mo maximum).")
                chunks.append(chunk)
            return 200, b''.join(chunks)
        finally:
            r.close()
    except requests.RequestException as e:
        raise SkillImportError(f"Téléchargement impossible ({type(e).__name__}).")
    finally:
        session.close()


def _github_default_branch(owner, repo):
    try:
        status, body = _safe_get(f"https://api.github.com/repos/{owner}/{repo}",
                                 accept='application/vnd.github+json')
        if status == 200:
            return str(json.loads(body).get('default_branch') or '')
    except (SkillImportError, ValueError):
        pass
    return ''


def fetch_git_skills(url, ref='', path=''):
    """Downloads a repository archive and returns (skills, skipped, source)."""
    src = parse_git_url(url, ref, path)
    archives = src['archives']
    if 'github' in src and not src['ref']:
        branch = _github_default_branch(*src['github'])
        if branch:
            owner, repo = src['github']
            archives = [(f"https://codeload.github.com/{owner}/{repo}/zip/{quote(branch)}", branch)]
    last = None
    for archive, archive_ref in archives:
        status, data = _safe_get(archive)
        if status == 200:
            src['ref'] = archive_ref
            skills, skipped = skills_from_zip(data, src['path'])
            return skills, skipped, {'url': src['canonical'], 'ref': src['ref'],
                                     'path': src['path']}
        last = status
    if last == 404:
        raise SkillImportError("Dépôt ou branche introuvable (dépôt privé ?).")
    raise SkillImportError(f"Le dépôt a répondu HTTP {last}.")


# ── Storage ───────────────────────────────────────────────────────────────

def save_skill(db, username, skill, source='manual', source_url=None, source_ref=None,
               source_path=None, replace=False):
    """Inserts (or replaces, when `replace`) a skill and its files.
    Returns (skill_id, None) or (None, reason)."""
    now = datetime.now().isoformat(timespec='seconds')
    row = db.execute("SELECT id FROM skills WHERE username=? AND name=?",
                     (username, skill['name'])).fetchone()
    if row and not replace:
        return None, "existe déjà"
    if row:
        skill_id = row['id']
        db.execute("UPDATE skills SET description=?, instructions=?, source=?, source_url=?, "
                   "source_ref=?, source_path=?, version=?, synced_at=? WHERE id=?",
                   (skill['description'], skill['instructions'], source, source_url, source_ref,
                    source_path, skill.get('version') or '', now, skill_id))
        db.execute("DELETE FROM skill_files WHERE skill_id=?", (skill_id,))
    else:
        cur = db.execute(
            "INSERT INTO skills (username, name, description, instructions, created_at, source, "
            "source_url, source_ref, source_path, version, synced_at, enabled) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,1)",
            (username, skill['name'], skill['description'], skill['instructions'], now, source,
             source_url, source_ref, source_path, skill.get('version') or '', now))
        skill_id = cur.lastrowid
    for path, content in (skill.get('files') or {}).items():
        db.execute("INSERT INTO skill_files (skill_id, username, path, content) VALUES (?,?,?,?)",
                   (skill_id, username, path, content))
    return skill_id, None


def skill_files(db, skill_id):
    return {r['path']: r['content'] for r in db.execute(
        "SELECT path, content FROM skill_files WHERE skill_id=? ORDER BY path", (skill_id,))}


def manual_skill_md(name, description, instructions):
    """The SKILL.md of a skill written in the interface: same shape as an
    imported one, so it downloads and re-imports identically."""
    desc = description.replace('\n', ' ').replace('"', "'")
    return f'---\nname: {name}\ndescription: "{desc}"\n---\n\n{instructions.strip()}\n'


# ── What the model sees ───────────────────────────────────────────────────
# Progressive disclosure, as in Claude Code: the catalog (name + description)
# goes in the tool description; `use_skill` returns the instructions and the
# list of bundled files; `read_skill_file` returns one file on demand.

SKILL_FILE_MAX_CHARS = 40_000


def enabled_skills(db, username):
    return db.execute("SELECT id, name, description FROM skills "
                      "WHERE username=? AND enabled=1 ORDER BY name", (username,)).fetchall()


def skill_tools(rows):
    """The two skill tools for `rows` (enabled skills), or []."""
    if not rows:
        return []
    catalog = "\n".join(f"- {r['name']}: {r['description']}" for r in rows)[:6000]
    names = [r['name'] for r in rows]
    return [
        {'type': 'function', 'function': {
            'name': 'use_skill',
            'description': ("Load the instructions of one of the user's skills when the task "
                            "matches its description. Available skills:\n" + catalog),
            'parameters': {'type': 'object', 'properties': {
                'name': {'type': 'string', 'enum': names}}, 'required': ['name']}}},
        {'type': 'function', 'function': {
            'name': 'read_skill_file',
            'description': ("Read a file bundled with a skill (script, reference, template), "
                            "as listed by use_skill."),
            'parameters': {'type': 'object', 'properties': {
                'name': {'type': 'string', 'enum': names},
                'path': {'type': 'string'}}, 'required': ['name', 'path']}}},
    ]


def exec_use_skill(db, username, name):
    """(text, ok): the instructions + the list of the skill's files."""
    row = db.execute("SELECT id, instructions FROM skills WHERE username=? AND name=? "
                     "AND enabled=1", (username, name)).fetchone()
    if not row:
        return f"Skill {name!r} not found.", False
    paths = [r['path'] for r in db.execute(
        "SELECT path FROM skill_files WHERE skill_id=? ORDER BY path", (row['id'],))
             if r['path'].lower() != 'skill.md']
    files = ("\n\nFiles bundled with this skill (read them with read_skill_file when the "
             "instructions point to them):\n" + "\n".join(f"- {p}" for p in paths)) if paths else ""
    return f"# Skill: {name}\n\n{row['instructions']}{files}", True


def exec_read_skill_file(db, username, name, path):
    row = db.execute("SELECT id FROM skills WHERE username=? AND name=? AND enabled=1",
                     (username, name)).fetchone()
    if not row:
        return f"Skill {name!r} not found.", False
    path = str(path or '').strip().lstrip('./')
    f = db.execute("SELECT content FROM skill_files WHERE skill_id=? AND path=?",
                   (row['id'], path)).fetchone()
    if not f:
        known = [r['path'] for r in db.execute(
            "SELECT path FROM skill_files WHERE skill_id=? ORDER BY path", (row['id'],))]
        return f"No file {path!r} in skill {name!r}. Files: {', '.join(known) or 'none'}.", False
    content = f['content']
    if len(content) > SKILL_FILE_MAX_CHARS:
        content = content[:SKILL_FILE_MAX_CHARS] + "\n[… truncated]"
    return f"{name}/{path}:\n{content}", True


# Words too common to say a skill applies.
_STOP = set("""avec dans pour sans sur sous une des les aux est sont cette ceci cela that this
with from into when what your have will about which their there using used file files
fichier fichiers faire fais peux veux code texte the and for are you not but can all
les des une est pas que qui son ses mes tes nos vos leur moi toi lui elle ils par""".split())


def _words(text):
    import unicodedata
    text = unicodedata.normalize('NFD', str(text or '').lower())
    text = ''.join(c for c in text if unicodedata.category(c) != 'Mn')
    return {w for w in re.findall(r'[a-z0-9]{3,}', text) if w not in _STOP}


def matching_skills(rows, request_text):
    """The enabled skills a request plausibly needs: named outright, or
    sharing at least two meaningful words with the description. Only then is
    a tool round paid; the model still decides whether to load one."""
    words = _words(request_text)
    lowered = str(request_text or '').lower()
    out = []
    for r in rows:
        name = r['name']
        if name in lowered or name.replace('-', ' ') in lowered:
            out.append(r)
            continue
        if len(words & (_words(r['description']) | _words(name.replace('-', ' ')))) >= 2:
            out.append(r)
    return out
