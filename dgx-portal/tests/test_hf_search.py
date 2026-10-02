"""Model search on Hugging Face: what the UI says about it must be true.

Written on 2026-09-13, after observing live that `search_hf_models`
swallowed EVERY exception to return `[]`. Two consequences, both checked
in the code before the fix:

1. **« HF est injoignable » displayed as « aucun modèle ne correspond ».**
   The user looking for a model concluded it did not exist. The function
   now raises `HfIndisponible` and `/api/search` answers 502 with
   `{ok: false, error}` — the admin actions contract.
2. **The GB10 filter made a misleading « aucun résultat ».** The frequent
   case is not « ce modèle n'existe pas » but « il n'est pas taggé gb10 ».
   The answer thus carries `hors_gb10` (True / False / None = we do not
   know), which lets the UI offer the unlocking gesture instead of leaving
   the user at a dead end.

An unknown task filter makes HF answer 400: it is a call error, not an
outage, and the two must not carry the same status.

And `POST /request`, which answered `flash()` + 204 in ALL cases
(including « tu as déjà une demande en attente »): the page announced
« Demande envoyée ! » while no row existed. It now answers JSON with an honest status.
"""
import os
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

import requests
from werkzeug.security import generate_password_hash

import app as portal
import notify
import vllm_health
from vllm_health import HfIndisponible, hf_modele_hors_gb10, search_hf_models


def _reponse(payload, status=200, headers=None):
    r = MagicMock()
    r.ok = 200 <= status < 300
    r.status_code = status
    r.json.return_value = payload
    r.headers = headers or {}
    return r


MODELE = {'id': 'org/modele', 'modelId': 'org/modele', 'downloads': 10,
          'tags': ['text-generation', 'gb10', 'gguf'], 'gated': False}


class CompteDeTest(unittest.TestCase):
    """Creates the local account used by the tests.

    Necessary and not decorative: the account state is RE-READ at every
    kept request (`auth.etat_compte`), so a session carrying a name absent
    from `local_users` is refused — 401 on a JSON route, redirect on a
    page route. Without this line, all tests below would measure the guard
    instead of what they target.
    """

    NOM = 'ztest-hf'

    def _mkuser(self):
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("DELETE FROM local_users WHERE username=?", (self.NOM,))
            db.execute(
                "INSERT INTO local_users (username, password_hash, fullname, is_admin, "
                "group_name, max_budget, enabled, created_at) VALUES (?,?,?,0,NULL,NULL,1,?)",
                (self.NOM, generate_password_hash('MotDePasse123'), 'Chercheur de test', 0))
            db.commit()

    def _rmuser(self):
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("DELETE FROM user_sessions WHERE username=?", (self.NOM,))
            db.execute("DELETE FROM local_users WHERE username=?", (self.NOM,))
            db.commit()

    def _client(self):
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s['csrf'] = 'tok'
            s['username'] = self.NOM
            s['fullname'] = 'Chercheur de test'
            s['is_admin'] = False
            # `auth_at` is MANDATORY: without it `_session_expired` treats the
            # session as predating the expiry mechanism, thus expired — we would
            # measure the guard instead of the search.
            s['auth_at'] = int(time.time())
        return c


class RechercheHfTest(unittest.TestCase):
    def test_une_panne_reseau_leve_au_lieu_de_renvoyer_vide(self):
        with patch.object(vllm_health.requests, 'get',
                          side_effect=requests.ConnectionError('boom')):
            with self.assertRaises(HfIndisponible):
                search_hf_models('qwen')

    def test_un_statut_http_en_erreur_leve(self):
        for code in (401, 429, 500, 503):
            with self.subTest(code=code):
                with patch.object(vllm_health.requests, 'get',
                                  return_value=_reponse({'error': 'nope'}, status=code)):
                    with self.assertRaises(HfIndisponible):
                        search_hf_models('qwen')

    def test_une_reponse_illisible_leve(self):
        r = _reponse(None)
        r.json.side_effect = ValueError('pas du JSON')
        with patch.object(vllm_health.requests, 'get', return_value=r):
            with self.assertRaises(HfIndisponible):
                search_hf_models('qwen')

    def test_un_succes_annote_le_moteur_et_demande_les_champs_pleins(self):
        with patch.object(vllm_health.requests, 'get',
                          return_value=_reponse([dict(MODELE)])) as get:
            out = search_hf_models('qwen', 'text-generation', gb10_only=True, skip=24)
        self.assertEqual(len(out), 1)
        # GGUF → llama.cpp: this is the badge telling the user which engine
        # will serve the model.
        self.assertEqual(out[0]['engine'], 'llamacpp')
        params = get.call_args.kwargs['params']
        self.assertEqual(params['skip'], 24)
        self.assertEqual(params['limit'], vllm_health._SEARCH_PAGE_SIZE)
        # `full=true` brings `gated`: the failure cause really met on this repo
        # (the Flash-Next mmproj), which we want to see BEFORE launching.
        self.assertEqual(params['full'], 'true')
        # `siblings` is what `full=true` reports that is heaviest (58 % of the
        # response, measured) and nothing reads it: it must not come out of here.
        self.assertNotIn('siblings', out[0])

    def test_le_filtre_gb10_est_transmis_et_jamais_impose(self):
        with patch.object(vllm_health.requests, 'get',
                          return_value=_reponse([dict(MODELE)])) as get:
            search_hf_models('qwen', None, gb10_only=True)
            self.assertEqual(get.call_args.kwargs['params']['filter'], [vllm_health.GB10_TAG])
            # Without asking for it, NO filter: this is the 2026-09-14 fix.
            search_hf_models('qwen', None, gb10_only=False)
            self.assertNotIn('filter', get.call_args.kwargs['params'])
            # A task is still possible, but it adds instead of replacing: several
            # `filter` = AND on the HF side.
            search_hf_models('qwen', 'text-generation', gb10_only=True)
            self.assertEqual(sorted(get.call_args.kwargs['params']['filter']),
                             sorted(['text-generation', vllm_health.GB10_TAG]))

    def test_la_recherche_par_defaut_couvre_tout_hugging_face(self):
        """The reported case: « ornith-1.5 » (4 repos, including the GGUFs) is
        tagged neither `gb10` nor only `text-generation`. The two automatic
        filters made it unfindable, and nothing said so."""
        with patch.object(vllm_health.requests, 'get',
                          return_value=_reponse([dict(MODELE)])) as get:
            search_hf_models('ornith-1.5')
        params = get.call_args.kwargs['params']
        self.assertEqual(params['search'], 'ornith-1.5')
        self.assertNotIn('filter', params)
        self.assertEqual(params['sort'], 'downloads')

    def test_has_more_vient_de_l_en_tete_link_de_hf(self):
        """A full page does not PROVE there are others: `Link` says so."""
        lien = {'Link': '<https://huggingface.co/api/models?search=q&skip=48>; rel="next"'}
        with patch.object(vllm_health.requests, 'get',
                          return_value=_reponse([dict(MODELE)], headers=lien)):
            _, more = vllm_health.search_hf_models_page('q')
        self.assertTrue(more)
        with patch.object(vllm_health.requests, 'get',
                          return_value=_reponse([dict(MODELE)])):
            _, more = vllm_health.search_hf_models_page('q')
        self.assertFalse(more)

    def test_le_jeton_hf_part_en_entete_et_ne_sort_jamais(self):
        with tempfile.TemporaryDirectory() as d:
            chemin = os.path.join(d, 'hf_token')
            with patch.dict(os.environ, {'HF_TOKEN': ''}), \
                 patch.object(vllm_health, 'HF_TOKEN_FILE', chemin):
                # No file: anonymous search, no authorization header.
                self.assertFalse(vllm_health.hf_jeton_present())
                with patch.object(vllm_health.requests, 'get',
                                  return_value=_reponse([dict(MODELE)])) as get:
                    search_hf_models('qwen')
                self.assertEqual(get.call_args.kwargs['headers'], {})
                # Token present: it goes in a header, and only its PRESENCE is said.
                with open(chemin, 'w', encoding='utf-8') as f:
                    f.write('hf_jeton_de_test\n')
                self.assertTrue(vllm_health.hf_jeton_present())
                with patch.object(vllm_health.requests, 'get',
                                  return_value=_reponse([dict(MODELE)])) as get:
                    search_hf_models('qwen')
                self.assertEqual(get.call_args.kwargs['headers'],
                                 {'Authorization': 'Bearer hf_jeton_de_test'})

    def test_un_jeton_refuse_se_distingue_d_une_panne(self):
        """401 with a token = « remplace-le », not « HF est en panne »."""
        r = _reponse({'error': 'invalid token'}, status=401)
        with patch.object(vllm_health, '_hf_token', return_value='hf_perime'), \
             patch.object(vllm_health.requests, 'get', return_value=r):
            with self.assertRaises(HfIndisponible) as ctx:
                search_hf_models('qwen')
        self.assertIn('jeton', str(ctx.exception))
        with patch.object(vllm_health, '_hf_token', return_value=''), \
             patch.object(vllm_health.requests, 'get', return_value=r):
            with self.assertRaises(HfIndisponible) as ctx:
                search_hf_models('qwen')
        self.assertIn('401', str(ctx.exception))

    def test_hors_gb10_repond_vrai_faux_ou_doute(self):
        with patch.object(vllm_health.requests, 'get', return_value=_reponse([dict(MODELE)])):
            self.assertTrue(hf_modele_hors_gb10('qwen'))
        with patch.object(vllm_health.requests, 'get', return_value=_reponse([])):
            self.assertFalse(hf_modele_hors_gb10('qwen'))
        # A doubt must not display as a « non ».
        with patch.object(vllm_health.requests, 'get',
                          side_effect=requests.ConnectionError('boom')):
            self.assertIsNone(hf_modele_hors_gb10('qwen'))
        # Without a query, the question is meaningless: nothing to say.
        self.assertIsNone(hf_modele_hors_gb10(''))


class ApiSearchTest(CompteDeTest):
    def setUp(self):
        portal.app.config['TESTING'] = True
        self._mkuser()
        self.c = self._client()

    def tearDown(self):
        self._rmuser()

    def test_panne_de_hf_repond_502_et_non_zero_resultat(self):
        with patch.object(vllm_health.requests, 'get',
                          side_effect=requests.ConnectionError('boom')):
            r = self.c.get('/api/search?q=qwen')
        self.assertEqual(r.status_code, 502)
        corps = r.get_json()
        self.assertFalse(corps['ok'])
        self.assertIn('Hugging Face', corps['error'])
        self.assertEqual(corps['results'], [])
        # `code`: without it, an English-speaking reader reads only a French sentence.
        self.assertEqual(corps['code'], 'hf_indisponible')

    def test_tache_inconnue_refusee_en_400_et_non_en_panne(self):
        with patch.object(vllm_health.requests, 'get') as get:
            r = self.c.get('/api/search?q=qwen&task=pas-une-tache')
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.get_json()['ok'])
        get.assert_not_called()

    def test_recherche_normale_dit_ok_et_renvoie_les_resultats(self):
        with patch.object(vllm_health.requests, 'get', return_value=_reponse([dict(MODELE)])):
            r = self.c.get('/api/search?q=qwen')
        self.assertEqual(r.status_code, 200)
        corps = r.get_json()
        self.assertTrue(corps['ok'])
        self.assertEqual(len(corps['results']), 1)
        self.assertEqual(corps['results'][0]['engine'], 'llamacpp')
        # Without filter: the answer says so, and `hf_token` is only a boolean.
        self.assertFalse(corps['gb10_only'])
        self.assertIn('hf_token', corps)
        self.assertIsInstance(corps['hf_token'], bool)

    def test_la_recherche_sans_filtre_est_le_defaut_de_la_route(self):
        with patch.object(vllm_health.requests, 'get',
                          return_value=_reponse([dict(MODELE)])) as get:
            self.c.get('/api/search?q=ornith-1.5')
        self.assertNotIn('filter', get.call_args.kwargs['params'])
        # `all=1` (the old parameter that disabled the filter) is still accepted:
        # it must not reintroduce a filter either.
        with patch.object(vllm_health.requests, 'get',
                          return_value=_reponse([dict(MODELE)])) as get:
            self.c.get('/api/search?q=ornith-1.5&all=1')
        self.assertNotIn('filter', get.call_args.kwargs['params'])
        # And an EMPTY search explores the catalogue (the most downloaded)
        # instead of returning nothing.
        with patch.object(vllm_health.requests, 'get',
                          return_value=_reponse([dict(MODELE)])) as get:
            corps = self.c.get('/api/search').get_json()
        self.assertEqual(get.call_count, 1)
        self.assertEqual(len(corps['results']), 1)

    def test_filtre_gb10_vide_signale_que_ca_existe_ailleurs(self):
        # 1st call (gb10-filtered, ASKED for): nothing. 2nd call (no filter): a model.
        with patch.object(vllm_health.requests, 'get',
                          side_effect=[_reponse([]), _reponse([dict(MODELE)])]):
            r = self.c.get('/api/search?q=ornith&gb10=1')
        corps = r.get_json()
        self.assertTrue(corps['ok'])
        self.assertEqual(corps['results'], [])
        self.assertTrue(corps['hors_gb10'])

    def test_pas_de_second_appel_quand_le_filtre_trouve_quelque_chose(self):
        with patch.object(vllm_health.requests, 'get',
                          return_value=_reponse([dict(MODELE)])) as get:
            r = self.c.get('/api/search?q=qwen&gb10=1')
        self.assertEqual(get.call_count, 1)
        self.assertIsNone(r.get_json()['hors_gb10'])


class DemandeDeModeleTest(CompteDeTest):
    """POST /request: the page must no longer announce a send that did not happen."""

    def setUp(self):
        portal.app.config['TESTING'] = True
        self._mkuser()
        self.c = self._client()
        self._purge()

    def tearDown(self):
        self._purge()
        self._rmuser()

    def _purge(self):
        with portal.app.app_context():
            db = portal.get_db()
            db.execute("DELETE FROM model_requests WHERE username=?", (self.NOM,))
            db.commit()

    def _post(self, **form):
        return self.c.post('/request', data=form, headers={'X-CSRFToken': 'tok'})

    def test_identifiant_vide_refuse(self):
        r = self._post(model_id='   ')
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()['code'], 'identifiant_requis')

    def test_demande_enregistree_puis_duplicat_refuse(self):
        with patch.object(portal, 'notify_email', return_value=True), \
             patch.object(portal, 'notify_discord', return_value=True):
            r1 = self._post(model_id='org/ztest-modele', reason='parce que')
            r2 = self._post(model_id='org/ztest-modele')
        self.assertEqual(r1.status_code, 200)
        corps = r1.get_json()
        self.assertTrue(corps['ok'])
        self.assertTrue(corps['email_sent'])
        self.assertTrue(corps['discord_sent'])
        # Before: silent 204, so « Demande envoyée ! » even for this duplicate.
        self.assertEqual(r2.status_code, 409)
        self.assertFalse(r2.get_json()['ok'])
        self.assertEqual(r2.get_json()['code'], 'deja_en_attente')

    def test_aucun_canal_de_notification_avertit_sans_echouer(self):
        with patch.object(portal, 'notify_email', return_value=False), \
             patch.object(portal, 'notify_discord', return_value=False):
            r = self._post(model_id='org/ztest-modele-2')
        corps = r.get_json()
        # The request IS recorded: a warning, not an error.
        self.assertEqual(r.status_code, 200)
        self.assertTrue(corps['ok'])
        self.assertIn('warning', corps)


class NotifyDiscordTest(unittest.TestCase):
    """`notify_discord` returned nothing, so `discord_sent` lied."""

    def test_renvoie_faux_sans_webhook(self):
        with patch.object(notify, 'DISCORD_WH', ''):
            self.assertFalse(notify.notify_discord('m', 'u', 'U', 'r'))

    def test_renvoie_vrai_quand_le_webhook_repond(self):
        with patch.object(notify, 'DISCORD_WH', 'https://discord.invalid/hook'), \
             patch.object(notify.requests, 'post', return_value=_reponse({}, status=204)):
            self.assertTrue(notify.notify_discord('m', 'u', 'U', 'r'))

    def test_renvoie_faux_quand_le_webhook_echoue(self):
        with patch.object(notify, 'DISCORD_WH', 'https://discord.invalid/hook'), \
             patch.object(notify.requests, 'post', return_value=_reponse({}, status=500)):
            self.assertFalse(notify.notify_discord('m', 'u', 'U', 'r'))
        with patch.object(notify, 'DISCORD_WH', 'https://discord.invalid/hook'), \
             patch.object(notify.requests, 'post',
                          side_effect=requests.ConnectionError('boom')):
            self.assertFalse(notify.notify_discord('m', 'u', 'U', 'r'))


if __name__ == '__main__':
    unittest.main()
