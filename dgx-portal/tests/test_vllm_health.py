"""Sante du modele : ce que chaque moteur sait dire, et ce qu'il ne sait pas.

Les deux moteurs n'exposent pas les memes metriques, et la tuile « requetes
servies » a longtemps affiche pour llama.cpp un compteur de TOKENS (39 303
requetes annoncees pour 39 303 tokens generes). Corriger cela ne devait rien
changer a vLLM, qui publie un vrai compteur de requetes : ces tests figent la
frontiere pour que le chemin vLLM ne parte pas avec le prochain nettoyage.
"""
import time
import types
import unittest
from unittest import mock

import vllm_health


def _metrics_vllm(gen="1000", succes=("42", "8"), ttft=("12.5", "50")):
    """Rend un /metrics vLLM realiste : metriques etiquetees, lignes a sommer."""
    lignes = [
        'vllm:generation_tokens_total{model_name="m"} %s' % gen,
        'vllm:num_requests_running{model_name="m"} 2.0',
        'vllm:num_requests_waiting{model_name="m"} 1.0',
        'vllm:request_success_total{finished_reason="stop",model_name="m"} %s' % succes[0],
        'vllm:request_success_total{finished_reason="length",model_name="m"} %s' % succes[1],
        'vllm:time_to_first_token_seconds_sum{model_name="m"} %s' % ttft[0],
        'vllm:time_to_first_token_seconds_count{model_name="m"} %s' % ttft[1],
    ]
    return "\n".join(lignes) + "\n"


_METRICS_LLAMA = ("llamacpp:tokens_predicted_total 39080\n"
                  "llamacpp:tokens_predicted_seconds_total 1235.5\n"
                  "llamacpp:n_decode_total 39303\n"
                  "llamacpp:predicted_tokens_seconds 0\n"
                  "llamacpp:requests_processing 0\n"
                  "llamacpp:requests_deferred 0\n")


def _sante(engine, texte):
    """Appelle vllm_health() en faisant croire que `engine` sert `texte`."""
    ligne = {'engine': engine}
    faux_db = types.SimpleNamespace(
        execute=lambda *a, **k: types.SimpleNamespace(fetchone=lambda: ligne))
    vllm_health._vllm_health_cache['t'] = 0.0
    with mock.patch.object(vllm_health, 'get_running_models', return_value=['m']), \
         mock.patch.object(vllm_health, 'get_db', return_value=faux_db), \
         mock.patch.object(vllm_health.requests, 'get',
                           return_value=types.SimpleNamespace(text=texte)):
        return vllm_health.vllm_health()


class SanteVllmTest(unittest.TestCase):
    """vLLM publie tout ce qu'il faut : rien de son affichage ne doit bouger."""

    def setUp(self):
        vllm_health._vllm_tps.update(t=0.0, gen=0.0)

    def test_compteur_de_requetes_somme_les_etiquettes(self):
        d = _sante('vllm', _metrics_vllm())
        self.assertEqual(d['requests'], 50)      # 42 "stop" + 8 "length"
        self.assertEqual(d['running'], 2)
        self.assertEqual(d['waiting'], 1)

    def test_ttft_vient_de_l_histogramme_du_moteur(self):
        d = _sante('vllm', _metrics_vllm())
        self.assertEqual(d['ttft'], 0.25)        # 12.5 s / 50 requetes

    def test_debit_calcule_en_delta_sur_deux_releves(self):
        self.assertIsNone(_sante('vllm', _metrics_vllm("1000"))['tps'])
        time.sleep(1.05)
        tps = _sante('vllm', _metrics_vllm("1300"))['tps']
        self.assertIsNotNone(tps)
        self.assertTrue(250 < tps < 350, tps)    # ~300 tokens en ~1 s

    def test_vllm_sans_trafic_n_herite_pas_du_ttft_d_un_autre_moteur(self):
        """Le repli « mesure par le portail » est reserve a llama.cpp.

        Sur `ttft_cnt == 0` il afficherait, pour un vLLM tout juste lance, le
        TTFT laisse par le moteur precedent.
        """
        with mock.patch('stats.ttft_mesure', return_value=9.99):
            d = _sante('vllm', _metrics_vllm("0", succes=("0", "0"), ttft=("0", "0")))
        self.assertEqual(d['requests'], 0)
        self.assertIsNone(d['ttft'])


class SanteLlamacppTest(unittest.TestCase):
    """llama.cpp en sait moins : il doit se taire plutot que d'inventer."""

    def test_pas_de_compteur_de_requetes_invente(self):
        self.assertIsNone(_sante('llamacpp', _METRICS_LLAMA)['requests'])

    def test_ttft_repris_de_la_mesure_du_portail(self):
        with mock.patch('stats.ttft_mesure', return_value=0.27):
            self.assertEqual(_sante('llamacpp', _METRICS_LLAMA)['ttft'], 0.27)

    def test_debit_lu_sur_n_decode_total_le_seul_a_avancer(self):
        """Pendant une generation la jauge vaut 0 et tokens_predicted_total ne
        bouge pas : seul n_decode_total avance, et il agrege tous les slots."""
        vllm_health._llama_tps.update(t=0.0, dec=None)
        base = ("llamacpp:tokens_predicted_total 39080\n"
                "llamacpp:tokens_predicted_seconds_total 1235.5\n"
                "llamacpp:predicted_tokens_seconds 0\n"
                "llamacpp:requests_processing 2\n"
                "llamacpp:requests_deferred 0\n")
        _sante('llamacpp', base + "llamacpp:n_decode_total 1000\n")
        time.sleep(1.05)
        tps = _sante('llamacpp', base + "llamacpp:n_decode_total 1030\n")['tps']
        self.assertTrue(25 < tps < 35, tps)      # ~30 tokens en ~1 s

    def test_debit_retombe_a_zero_quand_personne_ne_genere(self):
        """Compteur immobile entre deux releves = plus personne ne genere.

        On affichait le dernier debit connu, ce qui se lisait comme un compteur
        fige alors que la machine ne faisait rien.
        """
        vllm_health._llama_tps.update(t=0.0, dec=None)
        base = ("llamacpp:tokens_predicted_total 39080\n"
                "llamacpp:tokens_predicted_seconds_total 1235.5\n"
                "llamacpp:predicted_tokens_seconds 0\n"
                "llamacpp:requests_deferred 0\n")
        actif = base + "llamacpp:requests_processing 1\n"
        repos = base + "llamacpp:requests_processing 0\n"
        _sante('llamacpp', actif + "llamacpp:n_decode_total 1000\n")
        time.sleep(1.05)
        # une generation a eu lieu : debit non nul
        self.assertTrue(_sante('llamacpp', actif + "llamacpp:n_decode_total 1030\n")['tps'] > 0)
        time.sleep(1.05)
        # le compteur n'a plus bouge : personne n'utilise le modele
        self.assertEqual(_sante('llamacpp', repos + "llamacpp:n_decode_total 1030\n")['tps'], 0.0)


if __name__ == '__main__':
    unittest.main()
