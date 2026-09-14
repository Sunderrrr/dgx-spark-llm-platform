"""Tests du callback LiteLLM d'activite en vol (`dgx-portal/litellm_inflight.py`).

Ce module tourne DANS le proxy (monte sous le nom `cronos_inflight`), mais sa
logique est critique : c'est la seule source qui nomme un utilisateur pendant
qu'il genere. Deux proprietes a figer, dont la premiere est non negociable :

  - une erreur d'ecriture ne doit JAMAIS remonter : une requete ne peut pas
    echouer parce que le suivi d'activite n'a pas pu ecrire ;
  - une ligne ouverte est retiree a la fin, en succes comme en echec, sinon le
    panneau afficherait indefiniment des sessions fantomes.
"""
import importlib.util
import os
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

# Le module vit dans dgx-portal/ (et non dans litellm/) parce que le conteneur de
# tests ne monte que ce dossier : c'est ce qui le garde couvert par la CI. LiteLLM,
# lui, le recoit monte sous le nom `cronos_inflight` — voir docker-compose.yml.
import litellm_inflight


def _charge(chemin_db):
    """Recharge le module avec un fichier de suivi jetable."""
    os.environ['CRONOS_INFLIGHT_DB'] = chemin_db
    importlib.reload(litellm_inflight)
    return litellm_inflight


def _kwargs(cle='c1', alias='nlerou-1783330573', user_id='nlerou', modele='auto-model'):
    return {'litellm_call_id': cle, 'model': modele,
            'litellm_params': {'metadata': {'litellm_call_id': cle,
                                            'user_api_key_alias': alias,
                                            'user_api_key_user_id': user_id}}}


class ActiviteEnVolTest(unittest.TestCase):

    def setUp(self):
        self.dossier = tempfile.mkdtemp()
        self.chemin = os.path.join(self.dossier, 'inflight.db')
        self.mod = _charge(self.chemin)

    def _lignes(self):
        conn = sqlite3.connect(self.chemin)
        try:
            return conn.execute('SELECT cle, alias, user_id, modele FROM en_vol').fetchall()
        finally:
            conn.close()

    def test_une_requete_ouverte_puis_fermee(self):
        self.assertEqual(self.mod._enregistre(_kwargs()), 1)
        self.assertEqual(self._lignes(), [('c1', 'nlerou-1783330573', 'nlerou', 'auto-model')])
        self.assertEqual(self.mod._retire(_kwargs()), 1)
        self.assertEqual(self._lignes(), [])

    def test_deux_requetes_concurrentes_sont_independantes(self):
        """4 sessions sur ce moteur : retirer l'une ne doit pas toucher l'autre."""
        self.mod._enregistre(_kwargs('c1', 'nlerou-1', 'nlerou'))
        self.mod._enregistre(_kwargs('c2', 'cestienne-1', 'cestienne'))
        self.mod._retire(_kwargs('c1', 'nlerou-1', 'nlerou'))
        self.assertEqual([l[0] for l in self._lignes()], ['c2'])

    def test_une_ligne_perimee_est_balayee(self):
        """Un client tue en plein vol ne declenche ni succes ni echec."""
        self.mod._enregistre(_kwargs('vieux'))
        conn = sqlite3.connect(self.chemin)
        conn.execute('UPDATE en_vol SET debut=?', (time.time() - 99999,))
        conn.commit()
        conn.close()
        self.mod._enregistre(_kwargs('recent'))
        self.assertEqual([l[0] for l in self._lignes()], ['recent'])

    def test_sans_identifiant_aucune_ecriture(self):
        self.assertEqual(self.mod._enregistre({'litellm_params': {}}), 0)
        self.assertEqual(self.mod._retire({'litellm_params': {}}), 0)

    def test_une_base_illisible_ne_fait_pas_echouer_la_requete(self):
        """Premiere regle : le suivi d'activite n'est jamais fatal."""
        with mock.patch.object(self.mod, '_ouvre', side_effect=RuntimeError('disque plein')):
            with self.assertRaises(RuntimeError):
                self.mod._enregistre(_kwargs())
            # Les hooks, eux, avalent l'erreur : c'est ce qui protege la requete.
            hooks = self.mod.ActiviteEnVol()
            import asyncio
            asyncio.run(hooks.async_log_pre_api_call('m', [], _kwargs()))
            asyncio.run(hooks.async_log_success_event(_kwargs(), None, None, None))
            asyncio.run(hooks.async_log_failure_event(_kwargs(), None, None, None))

    def test_les_hooks_ecrivent_et_retirent(self):
        import asyncio
        hooks = self.mod.ActiviteEnVol()
        asyncio.run(hooks.async_log_pre_api_call('auto-model', [], _kwargs()))
        self.assertEqual(len(self._lignes()), 1)
        asyncio.run(hooks.async_log_success_event(_kwargs(), None, None, None))
        self.assertEqual(self._lignes(), [])


if __name__ == '__main__':
    unittest.main()
