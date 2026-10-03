# -*- coding: utf-8 -*-
"""asr/server.py — la politique de chargement du modèle de dictée.

C'est ICI que vit toute l'histoire de reprise de la dictée (2026-10-03) : le
sidecar charge ses poids au démarrage ET retente à la demande
(`_charger_si_besoin`) — avant, un seul échec CUDA laissait `_pipe` à None
pour toujours et « relance la dictée » était impossible par construction.

Le fichier vit HORS de l'image du portail : run-tests.sh le monte en lecture
seule sous `tests.asr_sidecar`. FastAPI/torch/transformers sont épinglés avant
l'import — ce qui est testé, c'est la POLTIQUE (reprise, single-flight,
throttle), pas une runtime CUDA.

États verrouillés par ces tests :
- un échec de chargement n'est PAS définitif : la tentative suivante le reprend ;
- throttle : au plus une tentative par fenêtre (`_RETRY_S`) — le dictaphone
  interroge toutes les secondes ;
- single-flight : deux appelants concurrents ne font qu'UNE tentative ;
- une fois chargé, plus aucune tentative n'est émise.
"""
import importlib
import sys
import threading
import types
import unittest
from unittest import mock


def _stub(nom, **attributs):
    m = types.ModuleType(nom)
    for k, v in attributs.items():
        setattr(m, k, v)
    sys.modules[nom] = m
    return m


class _FastAPIStub:
    def __init__(self, *a, **k):
        pass

    def _deco(self, *a, **k):
        def wrap(f):
            return f
        return wrap

    on_event = post = get = _deco


def _charge_module(pipeline):
    """(Re)charge `tests.asr_sidecar` avec des dépendances simulées."""
    for nom in ('fastapi', 'fastapi.responses', 'starlette', 'starlette.concurrency',
                'torch', 'numpy', 'soundfile', 'transformers'):
        sys.modules.pop(nom, None)
    _stub('fastapi', FastAPI=_FastAPIStub, File=lambda *a, **k: None,
          Form=lambda *a, **k: None, UploadFile=object,
          HTTPException=type('HTTPException', (Exception,), {'__init__': lambda self, *a, **k: None}))
    _stub('fastapi.responses', JSONResponse=type('JSONResponse', (), {'__init__': lambda self, *a, **k: None}))
    _stub('starlette')
    _stub('starlette.concurrency', run_in_threadpool=lambda f, *a, **k: f(*a, **k))
    _stub('torch', cuda=types.SimpleNamespace(is_available=lambda: False),
          float16='f16', float32='f32')
    _stub('numpy', int16='int16')
    _stub('soundfile', read=lambda *a, **k: (None, 16000))
    _stub('transformers', pipeline=pipeline)
    sys.modules.pop('tests.asr_sidecar', None)
    return importlib.import_module('tests.asr_sidecar')


class ChargementRetentableTest(unittest.TestCase):
    """La politique `_charger_si_besoin` du sidecar de dictée."""

    def _module(self, pipeline):
        mod = _charge_module(pipeline)
        mod._pipe = None
        mod._load_error = None
        mod._last_attempt = 0.0
        return mod

    def test_un_echec_de_chargement_n_est_pas_definitif(self):
        """Le défaut du 2026-10-03 : un seul CUDA OOM et la dictée morte à
        jamais. Ici, la tentative suivante (fenêtre écoulée) REPREND."""
        appels = []
        def pipeline(*a, **k):
            appels.append(1)
            if len(appels) == 1:
                raise RuntimeError('CUDA error: out of memory')
            return 'PIPELINE-OK'
        mod = self._module(pipeline)
        self.assertFalse(mod._charger_si_besoin())          # 1er essai : échec
        self.assertIn('out of memory', mod._load_error)
        mod._RETRY_S = 0.0                                  # fenêtre écoulée
        self.assertTrue(mod._charger_si_besoin())           # 2e essai : succès
        self.assertEqual(len(appels), 2)
        self.assertIsNone(mod._load_error)                  # l'erreur s'efface

    def test_le_throttle_borne_les_tentatives(self):
        """Le dictaphone interroge toutes les secondes : sans throttle, chaque
        poll relancerait une allocation CUDA qui échoue."""
        appels = []
        def pipeline(*a, **k):
            appels.append(1)
            raise RuntimeError('CUDA error: out of memory')
        mod = self._module(pipeline)
        self.assertFalse(mod._charger_si_besoin())
        for _ in range(5):                                  # dans la fenêtre
            self.assertFalse(mod._charger_si_besoin())
        self.assertEqual(len(appels), 1)                    # une seule tentative

    def test_single_flight_deux_appelants_une_tentative(self):
        """Deux polls concurrents (le dictaphone + la sonde) ne font qu'UNE
        tentative de chargement, pas deux sur le même GPU."""
        appels = []
        verrou_appels = threading.Lock()
        def pipeline(*a, **k):
            with verrou_appels:
                appels.append(1)
            return 'PIPELINE-OK'
        mod = self._module(pipeline)
        mod._RETRY_S = 0.0
        resultats = []
        def appel():
            resultats.append(mod._charger_si_besoin())
        fils = [threading.Thread(target=appel) for _ in range(4)]
        for f in fils:
            f.start()
        for f in fils:
            f.join()
        self.assertEqual(len(appels), 1)                    # single-flight
        self.assertTrue(all(resultats))                     # tous voient « chargé »

    def test_charge_plus_aucune_tentative(self):
        appels = []
        def pipeline(*a, **k):
            appels.append(1)
            return 'PIPELINE-OK'
        mod = self._module(pipeline)
        mod._RETRY_S = 0.0
        self.assertTrue(mod._charger_si_besoin())
        self.assertTrue(mod._charger_si_besoin())
        self.assertEqual(len(appels), 1)


if __name__ == '__main__':
    unittest.main()
