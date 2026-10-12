"""Memory settings, as in Claude: search past conversations, keep sensitive
topics out, and the switches' defaults (everything on)."""
import json
import time
import unittest

import app as portal
import conversation_search as cs
import memory_routes as memory
from db import get_db

USER = 'zmemset-user'
OTHER = 'zmemset-other'
CSRF = 'memset-csrf'


def _conv(username, cid, title, messages, updated):
    get_db().execute(
        "INSERT INTO conversations (username, client_id, title, model, messages, updated_at) "
        "VALUES (?,?,?,?,?,?)", (username, cid, title, 'm', json.dumps(messages), updated))


class _Base(unittest.TestCase):

    def setUp(self):
        self._clean()

    def tearDown(self):
        self._clean()

    def _clean(self):
        with portal.app.app_context():
            db = get_db()
            for u in (USER, OTHER):
                db.execute("DELETE FROM conversations WHERE username=?", (u,))
                db.execute("DELETE FROM user_prefs WHERE username=?", (u,))
                memory._mem_purge(u)
            db.commit()

    def _client(self):
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s['username'] = USER
            s['auth_at'] = int(time.time())
            s['csrf'] = CSRF
        return c


class SettingsTest(_Base):

    def test_defaults_on_and_toggle(self):
        c = self._client()
        d = c.get('/api/memory').get_json()
        self.assertTrue(d['search_chats'])
        self.assertTrue(d['sensitive'])
        r = c.post('/api/memory/settings', json={'sensitive': False}, headers={'X-CSRFToken': CSRF})
        self.assertEqual(r.get_json(), {'ok': True, 'search_chats': True, 'sensitive': False})
        r = c.post('/api/memory/settings', json={'search_chats': False}, headers={'X-CSRFToken': CSRF})
        self.assertFalse(r.get_json()['search_chats'])
        self.assertFalse(c.get('/api/memory').get_json()['sensitive'])
        with portal.app.app_context():
            self.assertFalse(cs.search_enabled(USER))
            self.assertTrue(memory._mem_enabled(USER), "the main switch is untouched")

    def test_settings_need_csrf(self):
        r = self._client().post('/api/memory/settings', json={'sensitive': False})
        self.assertEqual(r.status_code, 400)


class SensitiveTest(_Base):

    def test_sensitive_fact_refused_only_when_off(self):
        with portal.app.app_context():
            args = {'subject': 'Alice', 'relation': 'a', 'fact': 'Suit un traitement contre la dépression'}
            _, ok = memory._exec_memory_tool('save_memory', args, USER)
            self.assertTrue(ok, "on by default: kept")
            memory._mem_purge(USER)
            memory._mem_set_setting(USER, 'memory_sensitive', False)
            msg, ok = memory._exec_memory_tool('save_memory', args, USER)
            self.assertFalse(ok)
            self.assertIn("sensitive", msg)
            _, ok = memory._exec_memory_tool(
                'save_memory', {'subject': 'vLLM', 'fact': 'Utilise vLLM 0.28'}, USER)
            self.assertTrue(ok, "an ordinary fact still goes through")

    def test_detector(self):
        for t in ("pratique la religion musulmane", "votes for the green party",
                  "diagnosed with diabetes", "son salaire est de 4000"):
            self.assertTrue(memory.looks_sensitive(t), t)
        for t in ("préfère Python à Go", "works on the DGX Spark", "aime le café"):
            self.assertFalse(memory.looks_sensitive(t), t)


class ConversationSearchTest(_Base):

    def _seed(self):
        _conv(USER, 'c1', 'Déploiement Traefik',
              [{'role': 'user', 'content': 'Comment configurer le middleware ipallowlist de Traefik ?'},
               {'role': 'assistant', 'content': 'Ajoute un middleware ipAllowList avec sourceRange…'}],
              '2026-10-01T10:00:00')
        _conv(USER, 'c2', 'Recette', [{'role': 'user', 'content': 'Une recette de crêpes'}],
              '2026-10-05T10:00:00')
        _conv(OTHER, 'c3', 'Traefik secret',
              [{'role': 'user', 'content': 'Traefik middleware de OTHER'}], '2026-10-06T10:00:00')
        get_db().commit()

    def test_search_finds_own_conversations_with_snippet(self):
        with portal.app.app_context():
            self._seed()
            out = cs.search(USER, 'traefik middleware')
            self.assertIn('Déploiement Traefik', out)
            self.assertIn('ipallowlist', out)
            self.assertNotIn('OTHER', out, "never another account's history")
            self.assertIn('No past conversation', cs.search(USER, 'kubernetes'))

    def test_recent_lists_latest_first(self):
        with portal.app.app_context():
            self._seed()
            out = cs.recent(USER, 5)
            self.assertLess(out.index('Recette'), out.index('Déploiement Traefik'))
            self.assertIn('crêpes', out)

    def test_hidden_messages_are_not_searched(self):
        with portal.app.app_context():
            _conv(USER, 'c4', 'T', [{'role': 'user', 'content': 'zanzibar', 'hidden': True}],
                  '2026-10-07T10:00:00')
            get_db().commit()
            self.assertIn('No past conversation', cs.search(USER, 'zanzibar'))

    def test_past_reference_detection(self):
        ask = lambda t: cs.refers_to_past([{'role': 'user', 'content': t}])
        for t in ("Tu te souviens de ce qu'on avait dit sur Traefik ?",
                  "la dernière fois tu m'as donné un script",
                  "as we discussed, continue", "in our previous conversation"):
            self.assertTrue(ask(t), t)
        for t in ("Écris un script bash", "explain Traefik"):
            self.assertFalse(ask(t), t)

    def test_support_offers_the_tools_only_when_enabled(self):
        import support
        from unittest.mock import patch
        with portal.app.app_context():
            with patch.object(support, 'list_tools_cached', return_value=[]):
                _, routing = support._user_extra_tools(USER)
            self.assertEqual(routing.get('search_conversations'), {'kind': 'chats'})
            memory._mem_set_setting(USER, 'memory_search_chats', False)
            with patch.object(support, 'list_tools_cached', return_value=[]):
                _, routing = support._user_extra_tools(USER)
            self.assertNotIn('search_conversations', routing)


if __name__ == '__main__':
    unittest.main()
