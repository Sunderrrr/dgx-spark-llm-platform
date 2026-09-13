"""Recherche de modèles sur Hugging Face : ce que l'interface en dit doit être vrai.

Écrit le 2026-09-13, après avoir constaté en direct que `search_hf_models`
avalait TOUTE exception pour renvoyer `[]`. Deux conséquences, toutes deux
vérifiées dans le code avant correction :

1. **« HF est injoignable » s'affichait comme « aucun modèle ne correspond ».**
   L'utilisateur qui cherchait un modèle concluait qu'il n'existait pas. La
   fonction lève maintenant `HfIndisponible` et `/api/search` répond 502 avec
   `{ok: false, error}` — le contrat des actions d'admin.
2. **Le filtre GB10 rendait un « aucun résultat » trompeur.** Le cas fréquent
   n'est pas « ce modèle n'existe pas » mais « il n'est pas taggé gb10 ». La
   réponse porte donc `hors_gb10` (True / False / None = on ne sait pas), ce qui
   permet à l'interface de proposer le geste qui débloque au lieu de laisser
   l'utilisateur devant une impasse.

Un filtre de tâche inconnu fait répondre 400 à HF : c'est une erreur d'appel, pas
une panne, et les deux ne doivent pas porter le même statut.

Et `POST /request`, qui répondait `flash()` + 204 en TOUS les cas (y compris
« tu as déjà une demande en attente ») : la page annonçait « Demande envoyée ! »
sans qu'aucune ligne n'existe. Il répond désormais du JSON avec un statut honnête.
"""
import time
import unittest
from unittest.mock import MagicMock, patch

import requests
from werkzeug.security import generate_password_hash

import app as portal
import notify
import vllm_health
from vllm_health import HfIndisponible, hf_modele_hors_gb10, search_hf_models


def _reponse(payload, status=200):
    r = MagicMock()
    r.ok = 200 <= status < 300
    r.status_code = status
    r.json.return_value = payload
    return r


MODELE = {'id': 'org/modele', 'modelId': 'org/modele', 'downloads': 10,
          'tags': ['text-generation', 'gb10', 'gguf'], 'gated': False}


class CompteDeTest(unittest.TestCase):
    """Crée le compte local utilisé par les tests.

    Nécessaire et non décoratif : l'état du compte est RELU à chaque requête
    gardée (`auth.etat_compte`), donc une session portant un nom absent de
    `local_users` est refusée — 401 sur une route JSON, redirection sur une route
    de page. Sans cette ligne, tous les tests ci-dessous mesureraient la garde au
    lieu de ce qu'ils visent.
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
            # `auth_at` est OBLIGATOIRE : sans lui `_session_expired` traite la
            # session comme antérieure au mécanisme d'expiration, donc expirée —
            # on mesurerait la garde au lieu de la recherche.
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
        # GGUF → llama.cpp : c'est ce badge qui dit à l'utilisateur quel moteur
        # servira le modèle.
        self.assertEqual(out[0]['engine'], 'llamacpp')
        params = get.call_args.kwargs['params']
        self.assertEqual(params['skip'], 24)
        self.assertEqual(params['limit'], vllm_health._SEARCH_PAGE_SIZE)
        # `full=true` apporte `gated` : la cause d'échec rencontrée en vrai sur ce
        # dépôt (le mmproj de Flash-Next), qu'on veut voir AVANT de lancer.
        self.assertEqual(params['full'], 'true')
        # `siblings` est ce que `full=true` rapporte de plus lourd (58 % de la
        # réponse, mesuré) et rien ne le lit : il ne doit pas ressortir d'ici.
        self.assertNotIn('siblings', out[0])

    def test_le_filtre_gb10_est_transmis(self):
        with patch.object(vllm_health.requests, 'get',
                          return_value=_reponse([dict(MODELE)])) as get:
            search_hf_models('qwen', 'text-generation', gb10_only=True)
            self.assertIn(vllm_health.GB10_TAG, get.call_args.kwargs['params']['filter'])
            search_hf_models('qwen', 'text-generation', gb10_only=False)
            self.assertNotIn(vllm_health.GB10_TAG, get.call_args.kwargs['params']['filter'])

    def test_hors_gb10_repond_vrai_faux_ou_doute(self):
        with patch.object(vllm_health.requests, 'get', return_value=_reponse([dict(MODELE)])):
            self.assertTrue(hf_modele_hors_gb10('qwen'))
        with patch.object(vllm_health.requests, 'get', return_value=_reponse([])):
            self.assertFalse(hf_modele_hors_gb10('qwen'))
        # Un doute ne doit pas s'afficher comme un « non ».
        with patch.object(vllm_health.requests, 'get',
                          side_effect=requests.ConnectionError('boom')):
            self.assertIsNone(hf_modele_hors_gb10('qwen'))
        # Sans requête, la question n'a pas de sens : rien à dire.
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
        # `code` : sans lui, un lecteur anglophone ne lit qu'une phrase française.
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

    def test_filtre_gb10_vide_signale_que_ca_existe_ailleurs(self):
        # 1er appel (filtré gb10) : rien. 2e appel (sans filtre) : un modèle.
        with patch.object(vllm_health.requests, 'get',
                          side_effect=[_reponse([]), _reponse([dict(MODELE)])]):
            r = self.c.get('/api/search?q=ornith')
        corps = r.get_json()
        self.assertTrue(corps['ok'])
        self.assertEqual(corps['results'], [])
        self.assertTrue(corps['hors_gb10'])

    def test_pas_de_second_appel_quand_le_filtre_trouve_quelque_chose(self):
        with patch.object(vllm_health.requests, 'get',
                          return_value=_reponse([dict(MODELE)])) as get:
            r = self.c.get('/api/search?q=qwen')
        self.assertEqual(get.call_count, 1)
        self.assertIsNone(r.get_json()['hors_gb10'])


class DemandeDeModeleTest(CompteDeTest):
    """POST /request : la page ne doit plus annoncer un envoi qui n'a pas eu lieu."""

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
        # Avant : 204 muet, donc « Demande envoyée ! » même pour ce doublon.
        self.assertEqual(r2.status_code, 409)
        self.assertFalse(r2.get_json()['ok'])
        self.assertEqual(r2.get_json()['code'], 'deja_en_attente')

    def test_aucun_canal_de_notification_avertit_sans_echouer(self):
        with patch.object(portal, 'notify_email', return_value=False), \
             patch.object(portal, 'notify_discord', return_value=False):
            r = self._post(model_id='org/ztest-modele-2')
        corps = r.get_json()
        # La demande EST enregistrée : un avertissement, pas une erreur.
        self.assertEqual(r.status_code, 200)
        self.assertTrue(corps['ok'])
        self.assertIn('warning', corps)


class NotifyDiscordTest(unittest.TestCase):
    """`notify_discord` ne renvoyait rien, donc `discord_sent` mentait."""

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
