"""Skills as Claude Code defines them: SKILL.md + files, imported from a
.zip or a Git repository, browsable, re-synchronizable, and offered to the
model with progressive disclosure (catalog → instructions → files).

The archive is treated as hostile: these tests lock the zip-slip, symlink
and size guards as much as the happy path.
"""
import io
import time
import unittest
import zipfile
from unittest.mock import patch

import app as portal
import skills_store as store
from db import get_db

SKILL_MD = """---
name: pdf-tools
description: >
  Extract text and tables from PDF files,
  fill PDF forms.
version: 1.2.0
---

# PDF tools

Use scripts/extract.py to read a PDF. See reference.md for forms.
"""


def _zip(entries, symlinks=()):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for path, content in entries.items():
            zf.writestr(path, content)
        for path, target in symlinks:
            info = zipfile.ZipInfo(path)
            info.external_attr = (0o120777 << 16)
            zf.writestr(info, target)
    return buf.getvalue()


class FrontMatterTest(unittest.TestCase):

    def test_folded_description_and_version(self):
        meta, body = store.parse_front_matter(SKILL_MD)
        self.assertEqual(meta['name'], 'pdf-tools')
        self.assertEqual(meta['description'], 'Extract text and tables from PDF files, fill PDF forms.')
        self.assertEqual(meta['version'], '1.2.0')
        self.assertTrue(body.startswith('# PDF tools'))

    def test_quoted_and_plain_multiline(self):
        meta, _ = store.parse_front_matter('---\nname: "x"\ndescription: first line\n  second line\n---\nbody')
        self.assertEqual(meta['name'], 'x')
        self.assertEqual(meta['description'], 'first line second line')

    def test_no_front_matter(self):
        self.assertEqual(store.parse_front_matter('just text'), ({}, 'just text'))

    def test_name_rules(self):
        self.assertEqual(store.normalize_name('My Skill_v2!'), 'my-skill-v2')
        self.assertEqual(len(store.normalize_name('a' * 100)), 64)


class ZipImportTest(unittest.TestCase):

    def test_single_skill_with_files(self):
        data = _zip({'pdf-tools/SKILL.md': SKILL_MD, 'pdf-tools/scripts/extract.py': 'print(1)\n',
                     'pdf-tools/reference.md': '# forms\n', 'pdf-tools/logo.png': b'\x89PNG\x00\x01'})
        skills, skipped = store.skills_from_zip(data)
        self.assertEqual(skipped, [])
        self.assertEqual(len(skills), 1)
        s = skills[0]
        self.assertEqual(s['name'], 'pdf-tools')
        self.assertEqual(s['version'], '1.2.0')
        self.assertEqual(sorted(s['files']), ['SKILL.md', 'reference.md', 'scripts/extract.py'],
                         "binary files are skipped, text files kept")

    def test_plugin_with_several_skills_and_github_prefix(self):
        data = _zip({
            'repo-main/.claude-plugin/plugin.json': '{"version": "1.2.0"}',
            'repo-main/README.md': '# plugin',
            'repo-main/skills/debug/SKILL.md': '---\nname: debug\ndescription: Debug a failing test\n---\nSteps.',
            'repo-main/skills/standup/SKILL.md': '---\nname: standup\ndescription: Write a standup\n---\nSteps.',
            'repo-main/skills/broken/SKILL.md': '---\nname: broken\n---\nno description',
        })
        skills, skipped = store.skills_from_zip(data)
        self.assertEqual(sorted(s['name'] for s in skills), ['debug', 'standup'])
        self.assertEqual([n for n, _ in skipped], ['broken'], "a refused folder is reported, not dropped")

    def test_subpath_selects_one_folder(self):
        data = _zip({'r-main/skills/a/SKILL.md': '---\nname: a\ndescription: d a\n---\nx',
                     'r-main/skills/b/SKILL.md': '---\nname: b\ndescription: d b\n---\nx'})
        skills, _ = store.skills_from_zip(data, 'skills/b')
        self.assertEqual([s['name'] for s in skills], ['b'])
        with self.assertRaises(store.SkillImportError):
            store.skills_from_zip(data, 'skills/zzz')

    def test_zip_slip_and_symlinks_are_ignored(self):
        data = _zip({'s/SKILL.md': '---\nname: s\ndescription: d\n---\nx',
                     's/../../etc/evil.txt': 'pwned', '/abs/path.txt': 'pwned'},
                    symlinks=[('s/link', '/etc/passwd')])
        skills, _ = store.skills_from_zip(data)
        self.assertEqual(sorted(skills[0]['files']), ['SKILL.md'])

    def test_size_guards(self):
        with self.assertRaises(store.SkillImportError):
            store.skills_from_zip(b'x' * (store.MAX_ARCHIVE_BYTES + 1))
        with self.assertRaises(store.SkillImportError):
            store.skills_from_zip(b'not a zip')
        bomb = _zip({'s/SKILL.md': '---\nname: s\ndescription: d\n---\nx',
                     's/big.txt': 'a' * (store.MAX_UNCOMPRESSED + 1)})
        with self.assertRaises(store.SkillImportError):
            store.skills_from_zip(bomb)

    def test_no_skill_md(self):
        with self.assertRaises(store.SkillImportError):
            store.skills_from_zip(_zip({'README.md': 'hi'}))

    def test_download_roundtrip(self):
        files = {'SKILL.md': SKILL_MD, 'scripts/extract.py': 'print(1)\n'}
        skills, _ = store.skills_from_zip(store.build_zip('pdf-tools', files))
        self.assertEqual(skills[0]['files'], files)


class GitUrlTest(unittest.TestCase):

    def test_github_tree_url(self):
        src = store.parse_git_url('https://github.com/anthropics/skills/tree/main/document-skills/pdf')
        self.assertEqual(src['archives'], [('https://codeload.github.com/anthropics/skills/zip/main', 'main')])
        self.assertEqual(src['path'], 'document-skills/pdf')
        self.assertEqual(src['canonical'], 'https://github.com/anthropics/skills')

    def test_github_repo_without_branch_tries_main_then_master(self):
        src = store.parse_git_url('https://github.com/o/r.git')
        self.assertEqual([r for _, r in src['archives']], ['main', 'master'])

    def test_gitlab_and_gitea(self):
        src = store.parse_git_url('https://gitlab.com/group/sub/repo/-/tree/dev/skills')
        self.assertEqual(src['archives'], [('https://gitlab.com/group/sub/repo/-/archive/dev/repo-dev.zip', 'dev')])
        self.assertEqual(src['path'], 'skills')
        src = store.parse_git_url('https://codeberg.org/o/r/src/branch/main/x')
        self.assertEqual(src['archives'], [('https://codeberg.org/o/r/archive/main.zip', 'main')])
        self.assertEqual(src['path'], 'x')

    def test_refusals(self):
        for url in ('http://github.com/o/r', 'https://github.com/o', 'ftp://x/y/z'):
            with self.assertRaises(store.SkillImportError):
                store.parse_git_url(url)

    def test_download_goes_through_the_ssrf_guard(self):
        with patch.object(store, 'resolve_validated_mcp_ip',
                          return_value=(False, "Cet hôte n'est pas autorisé.", None)):
            with self.assertRaises(store.SkillImportError) as e:
                store.fetch_git_skills('https://github.com/o/r/tree/main')
        self.assertIn("pas autorisé", str(e.exception))

    def test_main_missing_falls_back_to_master(self):
        data = _zip({'r-master/SKILL.md': '---\nname: r\ndescription: d\n---\nx'})
        calls = []

        def _get(url, accept='application/zip'):
            calls.append(url)
            if 'api.github.com' in url:
                return 404, b''
            return (200, data) if url.endswith('/master') else (404, b'')

        with patch.object(store, '_safe_get', side_effect=_get):
            skills, _, src = store.fetch_git_skills('https://github.com/o/r')
        self.assertEqual(src['ref'], 'master')
        self.assertEqual([s['name'] for s in skills], ['r'])


class _Base(unittest.TestCase):
    CSRF = 'skills-test-csrf'
    USER = 'zskills-user'

    def setUp(self):
        self._clean()

    def tearDown(self):
        self._clean()

    def _clean(self):
        with portal.app.app_context():
            db = get_db()
            db.execute("DELETE FROM skill_files WHERE username=?", (self.USER,))
            db.execute("DELETE FROM skills WHERE username=?", (self.USER,))
            db.commit()

    def _client(self, username=None):
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s['username'] = username or self.USER
            s['auth_at'] = int(time.time())
            s['csrf'] = self.CSRF
        return c

    def _import(self, c, data, **form):
        return c.post('/api/skills/import', headers={'X-CSRFToken': self.CSRF},
                      data={'file': (io.BytesIO(data), 'skills.zip'), **form},
                      content_type='multipart/form-data')


class RoutesTest(_Base):

    def test_import_list_detail_download(self):
        c = self._client()
        data = _zip({'pdf-tools/SKILL.md': SKILL_MD, 'pdf-tools/scripts/extract.py': 'print(1)\n'})
        r = self._import(c, data)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual([s['name'] for s in r.get_json()['imported']], ['pdf-tools'])
        lst = c.get('/api/skills').get_json()['skills']
        self.assertEqual(lst[0]['source'], 'zip')
        self.assertEqual(lst[0]['version'], '1.2.0')
        self.assertEqual(lst[0]['file_count'], 2)
        sid = lst[0]['id']
        d = c.get(f'/api/skills/{sid}').get_json()['skill']
        self.assertEqual([f['path'] for f in d['files']], ['SKILL.md', 'scripts/extract.py'])
        z = c.get(f'/api/skills/{sid}/download')
        self.assertEqual(z.mimetype, 'application/zip')
        self.assertEqual(store.skills_from_zip(z.data)[0][0]['name'], 'pdf-tools')

    def test_existing_name_is_skipped_unless_replace(self):
        c = self._client()
        data = _zip({'pdf-tools/SKILL.md': SKILL_MD})
        self._import(c, data)
        r = self._import(c, data)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()['skipped'][0]['name'], 'pdf-tools')
        r = self._import(c, data, replace='1')
        self.assertEqual(r.status_code, 200)

    def test_bad_archive_says_why(self):
        r = self._import(self._client(), b'nope')
        self.assertEqual(r.status_code, 400)
        self.assertIn("zip", r.get_json()['error'])

    def test_isolation_between_accounts(self):
        self._import(self._client(), _zip({'pdf-tools/SKILL.md': SKILL_MD}))
        with portal.app.app_context():
            sid = get_db().execute("SELECT id FROM skills WHERE username=?", (self.USER,)).fetchone()['id']
        other = self._client('zskills-other')
        self.assertEqual(other.get(f'/api/skills/{sid}').status_code, 404)
        self.assertEqual(other.get(f'/api/skills/{sid}/download').status_code, 404)
        r = other.post(f'/api/skills/{sid}/enabled', json={'enabled': False},
                       headers={'X-CSRFToken': self.CSRF})
        self.assertEqual(r.status_code, 404)

    def test_enable_toggle_and_git_sync(self):
        c = self._client()
        data = _zip({'r-main/skills/debug/SKILL.md': '---\nname: debug\ndescription: Debug tests\n---\nv1'})
        with patch.object(store, '_safe_get', side_effect=lambda url, accept='': (
                (404, b'') if 'api.github.com' in url else (200, data))):
            r = c.post('/api/skills/import-git', headers={'X-CSRFToken': self.CSRF},
                       json={'url': 'https://github.com/o/r/tree/main/skills'})
        self.assertEqual(r.status_code, 200, r.get_json())
        sid = r.get_json()['imported'][0]['id']
        s = c.get(f'/api/skills/{sid}').get_json()['skill']
        self.assertEqual((s['source'], s['source_url'], s['source_ref'], s['source_path']),
                         ('git', 'https://github.com/o/r', 'main', 'skills'))
        v2 = _zip({'r-main/skills/debug/SKILL.md': '---\nname: debug\ndescription: Debug tests\n---\nv2'})
        with patch.object(store, '_safe_get', side_effect=lambda url, accept='': (200, v2)):
            r = c.post(f'/api/skills/{sid}/sync', headers={'X-CSRFToken': self.CSRF})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(c.get(f'/api/skills/{sid}').get_json()['skill']['instructions'], 'v2')
        r = c.post(f'/api/skills/{sid}/enabled', json={'enabled': False}, headers={'X-CSRFToken': self.CSRF})
        self.assertFalse(r.get_json()['enabled'])
        with portal.app.app_context():
            self.assertEqual(store.enabled_skills(get_db(), self.USER), [])

    def test_manual_skill_has_a_synthesized_skill_md(self):
        with portal.app.app_context():
            db = get_db()
            db.execute("INSERT INTO skills (username, name, description, instructions, created_at) "
                       "VALUES (?,?,?,?,?)", (self.USER, 'tone', 'Write in our tone', 'Be brief.', 'now'))
            db.commit()
            sid = db.execute("SELECT id FROM skills WHERE username=?", (self.USER,)).fetchone()['id']
        files = self._client().get(f'/api/skills/{sid}').get_json()['skill']['files']
        self.assertEqual(files[0]['path'], 'SKILL.md')
        self.assertIn('name: tone', files[0]['content'])


class ModelSideTest(_Base):
    """Progressive disclosure: catalog in the tool, then instructions, then files."""

    def _seed(self):
        c = self._client()
        self._import(c, _zip({'pdf-tools/SKILL.md': SKILL_MD,
                              'pdf-tools/scripts/extract.py': 'print("extract")\n'}))

    def test_tools_catalog_instructions_and_files(self):
        self._seed()
        with portal.app.app_context():
            db = get_db()
            rows = store.enabled_skills(db, self.USER)
            tools = store.skill_tools(rows)
            self.assertEqual([t['function']['name'] for t in tools], ['use_skill', 'read_skill_file'])
            self.assertIn('pdf-tools: Extract text', tools[0]['function']['description'])
            text, ok = store.exec_use_skill(db, self.USER, 'pdf-tools')
            self.assertTrue(ok)
            self.assertIn('# PDF tools', text)
            self.assertIn('- scripts/extract.py', text)
            text, ok = store.exec_read_skill_file(db, self.USER, 'pdf-tools', './scripts/extract.py')
            self.assertTrue(ok)
            self.assertIn('print("extract")', text)
            text, ok = store.exec_read_skill_file(db, self.USER, 'pdf-tools', 'nope.md')
            self.assertFalse(ok)
            self.assertIn('scripts/extract.py', text, "a miss lists the real files")
            _, ok = store.exec_use_skill(db, 'zskills-other', 'pdf-tools')
            self.assertFalse(ok, "another account's skill is invisible")

    def test_matching(self):
        rows = [{'name': 'pdf-tools', 'description': 'Extract text and tables from PDF files'},
                {'name': 'standup', 'description': 'Write the daily standup summary'}]
        names = lambda t: [r['name'] for r in store.matching_skills(rows, t)]
        self.assertEqual(names("extrais les tables de ce PDF"), ['pdf-tools'])
        self.assertEqual(names("use pdf-tools please"), ['pdf-tools'])
        self.assertEqual(names("write my daily standup"), ['standup'])
        self.assertEqual(names("bonjour"), [])

    def test_support_offers_enabled_skills_only(self):
        self._seed()
        import support
        with portal.app.app_context():
            with patch.object(support, 'list_tools_cached', return_value=[]):
                tools, routing = support._user_extra_tools(self.USER)
            self.assertIn('use_skill', routing)
            self.assertIn('read_skill_file', routing)
            get_db().execute("UPDATE skills SET enabled=0 WHERE username=?", (self.USER,))
            get_db().commit()
            with patch.object(support, 'list_tools_cached', return_value=[]):
                tools, routing = support._user_extra_tools(self.USER)
            self.assertNotIn('use_skill', routing)


class PlaygroundSkillsTest(_Base):

    def _run(self, body, phase_seen, final):
        from contextlib import ExitStack
        from flask import session
        import chat_routes as chat
        try:
            from test_file_tools import _scoped_requests
        except ImportError:
            from tests.test_file_tools import _scoped_requests

        def _phase(model, msgs, user_key, step_log, findings, **k):
            phase_seen.update(k)
            return
            yield

        class _Up:
            ok = True
            status_code = 200

            def iter_lines(self):
                yield b'data: {"choices":[{"delta":{"content":"ok"}}]}'
                yield b'data: [DONE]'

            def close(self):
                pass

        def _post(url, headers=None, json=None, stream=False, timeout=None):
            final['messages'] = json['messages']
            return _Up()

        with ExitStack() as stack:
            for name, value in (('maintenance_block_sse', None), ('_chat_rate_limited', None),
                                ('get_running_models', ['fake-model']),
                                ('get_user_keys', [{'key': 'sk-user'}]),
                                ('quota_exceeded_reset', None), ('_search_relevant', False),
                                ('image_available', False), ('_image_requested', False),
                                ('video_available', False), ('_video_requested', False),
                                ('document_available', False), ('_document_requested', False)):
                stack.enter_context(patch.object(chat, name, return_value=value))
            stack.enter_context(patch.object(chat.memory, '_mem_enabled', return_value=False))
            stack.enter_context(patch.object(chat, '_tools_phase', side_effect=_phase))
            stack.enter_context(patch.object(chat, 'requests', _scoped_requests(_post)))
            with portal.app.test_request_context('/playground/chat', method='POST', json=body):
                session['username'] = self.USER
                session['auth_at'] = int(time.time())
                chat.playground_chat().get_data(as_text=True)

    def test_explicit_skill_joins_the_system_and_arms_the_tools(self):
        self._import(self._client(), _zip({'pdf-tools/SKILL.md': SKILL_MD}))
        seen, final = {}, {}
        self._run({'messages': [{'role': 'user', 'content': 'go'}], 'skill': 'pdf-tools'}, seen, final)
        self.assertIn('# PDF tools', final['messages'][0]['content'])
        self.assertEqual([r['name'] for r in seen['skill_rows']], ['pdf-tools'])

    def test_matching_request_arms_the_tools_unrelated_does_not(self):
        self._import(self._client(), _zip({'pdf-tools/SKILL.md': SKILL_MD}))
        seen, final = {}, {}
        self._run({'messages': [{'role': 'user', 'content': 'extract the tables of this PDF'}]}, seen, final)
        self.assertTrue(seen.get('skill_rows'))
        seen, final = {}, {}
        self._run({'messages': [{'role': 'user', 'content': 'bonjour'}]}, seen, final)
        self.assertEqual(seen, {}, "no tool phase for an unrelated request")


if __name__ == '__main__':
    unittest.main()
