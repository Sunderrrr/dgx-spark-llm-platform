# -*- coding: utf-8 -*-
"""Routes utilisateur sans AUCUN test — trouvées par le scan de couverture
du 2026-10-03 (P1 : « zones utilisateur non protégées »).

Cohérent avec la règle maison : ce qui casse pour un utilisateur doit casser
un test AVANT de casser la production. Quatre familles :
- `/skills` : CRUD complet de compétences + isolation par compte ;
- `/playground/preview` : l'aperçu HTML « bac à sable » (sensible XSS) — un
  script ne doit jamais sortir de son bac ;
- `/internal/authcheck` : la porte du mode maintenance (forwardAuth de
  Traefik) — son contrat exact, les deux branches ;
- `/api/home` + `/api/modelhealth` : le schéma JSON que le tableau de bord
  consomme (une clé qui disparaît = un écran vide, silencieusement).
"""
import time
import unittest

import app as portal
import admin_routes  # noqa: F401  (monte les routes admin sur l'app)
import settings_routes  # noqa: F401
import preview_routes  # noqa: F401


class SessionTest(unittest.TestCase):
    CSRF = 'jeton-csrf-de-test'

    def _client(self, username='demo', admin=False):
        c = portal.app.test_client()
        with c.session_transaction() as s:
            s['username'] = username
            s['auth_at'] = int(time.time())
            s['csrf'] = self.CSRF
            s['is_admin'] = admin
        return c

    def _post(self, c, url, **data):
        return c.post(url, data=dict(data, csrf_token=self.CSRF),
                      headers={'X-CSRFToken': self.CSRF})


class CompetencesTest(SessionTest):
    """CRUD de `/skills` — aucune mention dans tests/ avant ce fichier."""

    def _cree(self, c, nom='ztest-competence'):
        return self._post(c, '/skills', action='create', name=nom,
                          description='pour les tests', instructions='fais ceci')

    def _noms(self, username):
        from db import get_db
        with portal.app.app_context():
            return [r['name'] for r in get_db().execute(
                "SELECT name FROM skills WHERE username=? ORDER BY id", (username,))]

    def _cherche(self, noeud, cle):
        """Trouve une clé où qu'elle soit nichée dans le payload."""
        if isinstance(noeud, dict):
            if cle in noeud:
                return noeud[cle]
            for v in noeud.values():
                r = self._cherche(v, cle)
                if r is not None:
                    return r
        elif isinstance(noeud, list):
            for v in noeud:
                r = self._cherche(v, cle)
                if r is not None:
                    return r
        return None

    def _ident(self, username):
        from db import get_db
        with portal.app.app_context():
            return get_db().execute(
                "SELECT id FROM skills WHERE username=?", (username,)).fetchone()['id']

    def test_cycle_complet_creation_lecture_suppression(self):
        c = self._client('alice')
        r = self._cree(c)
        self.assertTrue(r.get_json().get('ok'), r.get_json())
        self.assertEqual(self._noms('alice'), ['ztest-competence'])
        # Le compte la voit (payload /api/settings : skill_count + limits).
        params = c.get('/api/settings').get_json()
        self.assertEqual(self._cherche(params, 'skill_count'), 1)
        ident = self._ident('alice')
        r = self._post(c, '/skills', action='update', id=str(ident), name='renomme',
                       description='x', instructions='y')
        self.assertIn(r.status_code, (200, 204))
        self.assertEqual(self._noms('alice'), ['renomme'])   # la preuve est en base
        r = self._post(c, '/skills', action='delete', id=str(ident))
        self.assertIn(r.status_code, (200, 204))
        self.assertEqual(self._noms('alice'), [])

    def test_creation_incomplete_est_refusee(self):
        c = self._client('alice')
        r = self._post(c, '/skills', action='create', name='sans-rien')
        self.assertFalse(r.get_json().get('ok'))
        self.assertIn('requis', r.get_json().get('error', ''))

    def test_homonyme_dans_le_meme_compte_est_refuse(self):
        c = self._client('alice')
        self._cree(c)
        r = self._cree(c)                      # même nom, même compte
        self.assertFalse(r.get_json().get('ok'))
        self.assertIn('déjà', r.get_json().get('error', ''))

    def test_isolation_par_compte(self):
        """Un autre compte ne voit ni ne supprime ma compétence (le DELETE
        porte `WHERE username=?` — cette ligne le verrouille)."""
        c_alice = self._client('alice')
        self._cree(c_alice)
        ident = self._ident('alice')
        c_bob = self._client('bob')
        self._post(c_bob, '/skills', action='delete', id=str(ident))
        self.assertEqual(self._noms('alice'), ['ztest-competence'])
        self.assertEqual(self._noms('bob'), [])


class ApercuSandboxeTest(SessionTest):
    """`/playground/preview` : du HTML fourni par le MODÈLE, servi à
    l'utilisateur — la surface XSS de la plateforme. Le bac à sable est le
    seul rempart : ces assertions le verrouillent."""

    def test_apercu_est_servi_en_bac_a_sable(self):
        c = self._client()
        r = c.post('/playground/preview',
                   json={'html': '<h1>ok</h1><script>alert(1)</script>'},
                   headers={'X-CSRFToken': self.CSRF})
        self.assertIn(r.status_code, (200, 201))
        pid = r.get_json().get('id')
        self.assertTrue(pid, r.get_json())
        page = c.get(f'/playground/preview/{pid}')
        self.assertEqual(page.status_code, 200)
        csp = page.headers.get('Content-Security-Policy', '')
        self.assertIn('sandbox', csp)                       # origine opaque
        self.assertEqual(page.headers.get('X-Content-Type-Options'), 'nosniff')
        self.assertIn('<script>alert(1)</script>', page.get_data(as_text=True))

    def test_apercu_inconnu_dit_404(self):
        c = self._client()
        self.assertEqual(c.get('/playground/preview/introuvable').status_code, 404)


class AuthcheckMaintenanceTest(SessionTest):
    """`/internal/authcheck` : la porte que Traefik interroge pour le mode
    maintenance. Deux branches, deux contrats — aucun n'était testé."""

    def setUp(self):
        from db import get_db, set_setting
        with portal.app.app_context():
            db = get_db()
            db.execute("DELETE FROM settings WHERE key='maintenance_mode'")
            db.commit()
            set_setting('maintenance_mode', '0')

    def tearDown(self):
        from db import set_setting
        with portal.app.app_context():
            set_setting('maintenance_mode', '0')

    def test_hors_maintenance_tout_passe_sans_verification(self):
        r = portal.app.test_client().get('/internal/authcheck')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_data(), b'')                # aucun coût ajouté

    def test_en_maintenance_un_appelant_sans_cle_est_refuse(self):
        from db import set_setting
        with portal.app.app_context():
            set_setting('maintenance_mode', '1')
        r = portal.app.test_client().get('/internal/authcheck')
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r.get_json()['error']['type'], 'maintenance_mode')

    def test_en_maintenance_le_bearer_admin_passe(self):
        from db import get_db, set_setting
        ctx = portal.app.app_context()
        ctx.push()
        db = get_db()
        db.execute("INSERT INTO local_users (username, password_hash, is_admin, created_at) "
                   "VALUES ('ztest-adm', 'x', 1, '2026-10-03')",)
        db.execute("INSERT INTO api_keys (username, key_alias, key_value, created_at) "
                   "VALUES ('ztest-adm', 'ztest', 'sk-ztest-authcheck', '2026-10-03')")
        db.commit()
        try:
            set_setting('maintenance_mode', '1')
            r = portal.app.test_client().get(
                '/internal/authcheck',
                headers={'Authorization': 'Bearer sk-ztest-authcheck'})
            self.assertEqual(r.status_code, 200)
        finally:
            db.execute("DELETE FROM api_keys WHERE key_value='sk-ztest-authcheck'")
            db.execute("DELETE FROM local_users WHERE username='ztest-adm'")
            db.commit()
            ctx.pop()


class SchemaTableauDeBordTest(SessionTest):
    """Le schéma JSON de `/api/modelhealth` et `/api/home` : le tableau de
    bord lit ces clés directement — leur disparition ne lèverait qu'un écran
    vide, silencieusement. Le moteur est simulé : on teste LE SCHÉMA."""

    CLES_SANTE = ('running', 'tps', 'tps_moyen', 'tps_prefill', 'ttft',
                  'sessions', 'metrics')

    def test_modelhealth_porte_les_cles_du_tableau_de_bord(self):
        from unittest import mock
        sante = {k: 1.0 for k in self.CLES_SANTE}
        sante.update({'running': True, 'metrics': False})
        with mock.patch('app.vllm_health', return_value=sante):
            r = self._client().get('/api/modelhealth')
        self.assertEqual(r.status_code, 200)
        corps = r.get_json()
        for k in self.CLES_SANTE:
            self.assertIn(k, corps, f"clé disparue du tableau de bord : {k}")

    def test_home_repond_un_json_exploitable(self):
        r = self._client().get('/api/home')
        self.assertEqual(r.status_code, 200)
        self.assertIsInstance(r.get_json(), dict)


class MentionMemoirePartageeTest(unittest.TestCase):
    """Point produit 2, option (a) : les erreurs des services médias portent la
    mémoire réellement libre. Mesuré le 2026-10-03 : MiMo tient ~100 des
    121,6 Go — « le service est arrêté » seul faisait croire qu'un clic
    suffisait, alors qu'un sidecar DÉMARRÉ peut encore manquer de mémoire."""

    def test_la_mention_porte_la_memoire_libre(self):
        from unittest import mock
        from sidecars import mention_memoire_partagee
        with mock.patch('sidecars._mem_available_gb', return_value=9.7):
            m = mention_memoire_partagee()
        self.assertIn('9.7 Go libres', m)
        self.assertIn('Admin', m)

    def test_sans_mesure_il_n_y_a_pas_de_faux_chiffre(self):
        from unittest import mock
        from sidecars import mention_memoire_partagee
        with mock.patch('sidecars._mem_available_gb', return_value=None):
            self.assertEqual(mention_memoire_partagee(), "")

    def test_les_erreurs_media_la_portent(self):
        from unittest import mock
        with mock.patch('sidecars._mem_available_gb', return_value=8.0):
            with mock.patch('video_routes.comfyui_generate', return_value=None):
                c = SessionTest()._client()
                r = SessionTest()._post(c, '/api/video/generate', prompt='une vague')
        self.assertIn('Go libres', r.get_json().get('error', ''))


if __name__ == '__main__':
    unittest.main()
