# -*- coding: utf-8 -*-
"""asr/server.py — the dictation model's loading policy.

THIS is where the whole dictation-recovery story lives (2026-10-03): the
sidecar loads its weights at startup AND retries on demand
(`_charger_si_besoin`) — before, a single CUDA failure left `_pipe` at None
forever and « relance la dictée » was impossible by construction.

The file lives OUTSIDE the portal image: run-tests.sh mounts it read-only
as `tests.asr_sidecar`. FastAPI/torch/transformers are pinned before the
import — what is tested is the POLICY (recovery, single-flight,
throttle), not a CUDA runtime.

States locked in by these tests:
- a load failure is NOT final: the next attempt picks it up;
- throttle: at most one attempt per window (`_RETRY_S`) — the dictaphone
  polls every second;
- single-flight: two concurrent callers make ONE attempt only;
- once loaded, no further attempt is ever made.
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
    """(Re)loads `tests.asr_sidecar` with stubbed dependencies."""
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
    """The `_charger_si_besoin` policy of the dictation sidecar."""

    def _module(self, pipeline):
        mod = _charge_module(pipeline)
        mod._pipe = None
        mod._load_error = None
        mod._last_attempt = 0.0
        return mod

    def test_un_echec_de_chargement_n_est_pas_definitif(self):
        """The 2026-10-03 defect: a single CUDA OOM and the dictation dead
        forever. Here the next attempt (window elapsed) RESUMES."""
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
        """The dictaphone polls every second: without a throttle, each
        poll would retry a failing CUDA allocation."""
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
        """Two concurrent polls (the dictaphone + the probe) make ONE
        load attempt, not two on the same GPU."""
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
