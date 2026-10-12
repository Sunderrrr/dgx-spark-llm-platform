"""Playground file editing: targeted edits with a feedback loop.

The edit protocol removed on e955e10 failed SILENTLY: a mis-copied anchor
did not apply and nobody told the model. These tests lock what replaces it:
an edit applies only on an exact, unique match, and every miss is an error
the model reads — and can fix on its next round.
"""
import json
import time
import types
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from flask import session

import app as portal  # noqa: F401  (registers the routes)
import chat_routes as chat
import file_tools
import websearch_tools

def _scoped_requests(post):
    """A `requests` stand-in for ONE module: patching `requests.post` would
    patch the package shared by the whole process (test_regression_guards)."""
    import requests
    ns = types.SimpleNamespace(**{k: getattr(requests, k) for k in dir(requests)
                                  if not k.startswith('_')})
    ns.post = post
    return ns


SRC = "def greet(name):\n    return 'Hello, ' + name\n\nprint(greet('x'))\n"


class StrReplaceTest(unittest.TestCase):

    def setUp(self):
        self.files = {'app/greet.py': SRC}
        self.changes = {}

    def _run(self, name, **args):
        return file_tools.exec_file_tool(name, args, self.files, self.changes)

    def test_unique_match_is_replaced_and_snippet_returned(self):
        result, error = self._run('str_replace', path='app/greet.py',
                                  old_str="return 'Hello, ' + name",
                                  new_str="return f'Hello, {name}!'")
        self.assertIsNone(error)
        self.assertIn("return f'Hello, {name}!'", self.files['app/greet.py'])
        self.assertIn("Edited app/greet.py", result)
        self.assertIn("     2\t", result, "the snippet carries line numbers")
        self.assertEqual(self.changes, {'app/greet.py': SRC}, "the original is kept once")

    def test_missing_text_changes_nothing_and_says_why(self):
        result, error = self._run('str_replace', path='app/greet.py',
                                  old_str="return 'Hi, ' + name", new_str="x")
        self.assertEqual(error, "text not found")
        self.assertIn("not found", result)
        self.assertEqual(self.files['app/greet.py'], SRC)
        self.assertEqual(self.changes, {})

    def test_indentation_miss_gets_a_hint(self):
        result, _ = self._run('str_replace', path='app/greet.py',
                              old_str="def greet(name):\n  return 'Hello, ' + name",
                              new_str="x")
        self.assertIn("DIFFERENT indentation", result)

    def test_ambiguous_text_is_refused_with_line_numbers(self):
        self.files['a.txt'] = "x = 1\ny = 2\nx = 1\n"
        result, error = self._run('str_replace', path='a.txt', old_str="x = 1", new_str="x = 3")
        self.assertEqual(error, "ambiguous text")
        self.assertIn("2 times", result)
        self.assertIn("lines 1, 3", result)
        self.assertEqual(self.files['a.txt'], "x = 1\ny = 2\nx = 1\n")

    def test_suffix_path_designates_the_file(self):
        _, error = self._run('str_replace', path='greet.py', old_str="print", new_str="echo")
        self.assertIsNone(error)
        self.assertIn("echo(greet", self.files['app/greet.py'])

    def test_unknown_file_lists_the_known_ones(self):
        result, error = self._run('str_replace', path='nope.py', old_str="a", new_str="b")
        self.assertEqual(error, "unknown file")
        self.assertIn("app/greet.py", result)

    def test_several_edits_keep_the_first_original(self):
        self._run('str_replace', path='app/greet.py', old_str="greet(name)", new_str="greet(who)")
        self._run('str_replace', path='app/greet.py', old_str="+ name", new_str="+ who")
        self.assertEqual(self.changes['app/greet.py'], SRC)


class ViewAndCreateTest(unittest.TestCase):

    def test_view_numbers_lines_and_pages(self):
        files = {'big.txt': "\n".join(f"line {i}" for i in range(1, 1001))}
        result, error = file_tools.exec_file_tool('view_file', {'path': 'big.txt'}, files, {})
        self.assertIsNone(error)
        self.assertIn("     1\tline 1", result)
        self.assertIn("start_line=401", result, "a long file is paged, not cut silently")
        result, _ = file_tools.exec_file_tool(
            'view_file', {'path': 'big.txt', 'start_line': 990}, files, {})
        self.assertIn("  1000\tline 1000", result)

    def test_create_records_a_new_file(self):
        files, changes = {}, {}
        _, error = file_tools.exec_file_tool(
            'create_file', {'path': 'new.md', 'content': '# Title\n'}, files, changes)
        self.assertIsNone(error)
        self.assertEqual(files, {'new.md': '# Title\n'})
        self.assertEqual(changes, {'new.md': None}, "None = the file did not exist")


class BoundsAndTriggerTest(unittest.TestCase):

    def test_parse_files_bounds(self):
        raw = [{'name': 'a', 'content': 'x'}, {'name': '', 'content': 'y'},
               {'name': 'big', 'content': 'z' * (file_tools.MAX_FILE_CHARS + 1)},
               'junk', {'name': 'b', 'content': 3}]
        self.assertEqual(file_tools.parse_files(raw), {'a': 'x'})
        self.assertEqual(file_tools.parse_files('nope'), {})

    def test_edit_request_detection(self):
        ask = lambda t: file_tools.edit_requested([{'role': 'user', 'content': t}])
        for t in ("Change la couleur du titre", "corrige le bug ligne 3",
                  "rajoute un bouton", "please fix the loop", "rename the function",
                  # Follow-up corrections without an edit verb (seen in production).
                  "en faite pas traefik mais nginx proxy manager", "plutôt en Python",
                  "actually use port 8080 instead"):
            self.assertTrue(ask(t), t)
        for t in ("merci !", "explique-moi ce code", "what does it do?"):
            self.assertFalse(ask(t), t)

    def test_edits_note_only_for_real_changes(self):
        self.assertEqual(file_tools.edits_note({'a': 'x'}, {'a': 'x'}), '')
        self.assertIn("Do NOT output these files again",
                      file_tools.edits_note({'a': 'y'}, {'a': 'x'}))


class _Resp:
    ok = True
    status_code = 200

    def __init__(self, message):
        self._message = message

    def json(self):
        return {'choices': [{'message': self._message}]}


def _call(name, **args):
    return {'id': f'c-{name}', 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(args)}}


class ToolsPhaseLoopTest(unittest.TestCase):
    """The loop that makes edits reliable: a miss goes back to the model."""

    def test_a_miss_is_reported_then_corrected(self):
        rounds = [
            {'content': '', 'tool_calls': [_call('str_replace', path='greet.py',
                                                 old_str="return 'Hi'", new_str='x')]},
            {'content': '', 'tool_calls': [_call('str_replace', path='greet.py',
                                                 old_str="return 'Hello, ' + name",
                                                 new_str="return f'Hello, {name}!'")]},
            {'content': 'done'},
        ]
        sent = []

        def _post(url, headers=None, json=None, timeout=None):
            sent.append(json)
            return _Resp(rounds[len(sent) - 1])

        files, changes = {'greet.py': SRC}, {}
        with patch.object(websearch_tools, 'requests', _scoped_requests(_post)):
            steps = list(websearch_tools._tools_phase(
                'm', [{'role': 'user', 'content': 'change the greeting'}], 'sk', [], [],
                web_ok=False, files=files, changes=changes))
        self.assertEqual(len(sent), 3)
        # The second request carries the error of the first edit (the list
        # is shared across rounds: the FIRST tool message is that error).
        tool_msgs = [m for m in sent[1]['messages'] if m.get('role') == 'tool']
        self.assertIn("not found", tool_msgs[0]['content'])
        self.assertIn("f'Hello, {name}!'", files['greet.py'])
        self.assertEqual(changes, {'greet.py': SRC})
        names = [t['function']['name'] for t in sent[0]['tools']]
        self.assertEqual(names, ['view_file', 'str_replace', 'create_file'])
        self.assertTrue(any('file_edit' in s for s in steps))
        self.assertIn("Files of this conversation", json.dumps(sent[0]['messages']))


    def test_a_single_system_message_stays_first(self):
        """Qwen's template raises « System message must be at the beginning »
        (HTTP 500) on a second system message: the file list joins the first."""
        sent = []

        def _post(url, headers=None, json=None, timeout=None):
            sent.append(json)
            return _Resp({'content': 'nothing to do'})

        msgs = [{'role': 'system', 'content': 'You are helpful.'},
                {'role': 'user', 'content': 'add a network'}]
        with patch.object(websearch_tools, 'requests', _scoped_requests(_post)):
            list(websearch_tools._tools_phase('m', msgs, 'sk', [], [], web_ok=False,
                                              files={'greet.py': SRC}, changes={}))
        roles = [m['role'] for m in sent[0]['messages']]
        self.assertEqual(roles.count('system'), 1)
        self.assertEqual(roles[0], 'system')
        self.assertIn('You are helpful.', sent[0]['messages'][0]['content'])
        self.assertIn('Files of this conversation', sent[0]['messages'][0]['content'])


class PlaygroundWiringTest(unittest.TestCase):
    """The route arms the tools, sends each changed file back once, and
    tells the final answer not to reprint it."""

    def test_changed_file_goes_back_and_answer_is_told(self):
        seen = {}

        def _phase(model, msgs, user_key, step_log, findings, **k):
            seen.update(k)
            k['files']['greet.py'] = SRC.replace('Hello', 'Hi')
            k['changes']['greet.py'] = SRC
            return
            yield

        final = {}

        class _Up:
            ok = True
            status_code = 200

            def iter_lines(self):
                yield b'data: {"choices":[{"delta":{"content":"Done."}}]}'
                yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}'
                yield b'data: [DONE]'

            def close(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def _post(url, headers=None, json=None, stream=False, timeout=None):
            final['messages'] = json['messages']
            return _Up()

        body = {'messages': [{'role': 'user', 'content': 'change the greeting'}],
                'files': [{'name': 'greet.py', 'content': SRC}]}
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
                session['username'] = 'demo'
                session['auth_at'] = int(time.time())
                out = chat.playground_chat().get_data(as_text=True)
        self.assertEqual(seen['files'], {'greet.py': SRC.replace('Hello', 'Hi')})
        events = [json.loads(l[5:]) for l in out.split('\n') if l.startswith('data: {')]
        edits = [e['dgx_file'] for e in events if 'dgx_file' in e]
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0]['name'], 'greet.py')
        self.assertIn("'Hi, '", edits[0]['content'])
        self.assertFalse(edits[0]['created'])
        last_user = [m for m in final['messages'] if m['role'] == 'user'][-1]
        self.assertIn("ALREADY applied your edits to: greet.py", last_user['content'])

    def test_no_change_request_no_tools(self):
        seen = {}

        def _phase(*a, **k):
            seen['called'] = True
            return
            yield

        body = {'messages': [{'role': 'user', 'content': 'merci'}],
                'files': [{'name': 'greet.py', 'content': SRC}]}
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
            def _down(*a, **k):
                raise RuntimeError('no model here')
            stack.enter_context(patch.object(chat, 'requests', _scoped_requests(_down)))
            with portal.app.test_request_context('/playground/chat', method='POST', json=body):
                session['username'] = 'demo'
                session['auth_at'] = int(time.time())
                chat.playground_chat().get_data(as_text=True)
        self.assertNotIn('called', seen)


class ConversationKeepsEditsTest(unittest.TestCase):

    def test_file_edits_survive_a_save(self):
        import conversation_routes as conv
        m = {'role': 'assistant', 'content': 'Done.',
             'fileEdits': [{'name': 'greet.py', 'content': SRC}, {'name': '', 'content': 'x'}]}
        self.assertEqual(conv._file_edits(m), [{'name': 'greet.py', 'content': SRC}])
        self.assertEqual(conv._file_edits({**m, 'role': 'user'}), [])


if __name__ == '__main__':
    unittest.main()
