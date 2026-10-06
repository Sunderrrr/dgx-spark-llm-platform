# -*- coding: utf-8 -*-
"""Anti-regression guards for the patterns that cost us intermittence.

Two families have already failed pushes at random (the 2026-10-02 flake,
three aborted pushes); this file FREEZES their headcount: any addition
makes a test fail, with the explanation of what to do.

1. `threading.Thread(` in the portal: every background thread that calls
   the network during the suite becomes a zombie again (the global
   `requests` mocks catch it at random). THREE exist (budget reaper, ASR
   watcher, launch follower) and all three are behind the
   `CRONOS_NO_REAPER` seam. A fourth must be too.
2. `patch.object(<module>.requests, …)` in the tests: these mocks patch
   the `requests` package SHARED by the whole process — the exact cause
   of the flake. The existing sites are frozen; any new one should prefer
   a scoped namespace (cf. `_ReqMock` in test_asr_ondemand.py).
"""
import pathlib
import re
import unittest

RACINE = pathlib.Path(__file__).resolve().parent.parent


class ThreadsArrierePlanGelTest(unittest.TestCase):
    """No new long-lived thread without the test seam.

    Measured distinction (2026-10-03): 13 `threading.Thread(` live in the
    portal, but the 10 others are REQUEST-SCOPED (SSE reader, media job
    worker, e-mail sender) — they never leave a request, hence never a
    random test. The real zombies are the NAMED threads: they live from
    one import to the next and talk to the network on their own.
    """

    MAX_THREADS_NOMMES = 3   # reaper budget + veilleur ASR + suivi-lancement

    def test_effectif_des_threads_de_longue_vie_gel(self):
        sites, sans_couture = [], []
        for f in sorted(RACINE.glob('*.py')):
            texte = f.read_text()
            for n, ligne in enumerate(texte.splitlines(), 1):
                if 'threading.Thread(' in ligne and 'name=' in ligne:
                    sites.append(f"{f.name}:{n}")
                    if 'CRONOS_NO_REAPER' not in texte:
                        sans_couture.append(f"{f.name}:{n}")
        self.assertLessEqual(
            len(sites), self.MAX_THREADS_NOMMES,
            "Nouveau thread de longue vie détecté : " + ", ".join(sites) +
            " — il doit être derrière `CRONOS_NO_REAPER=1` (comme les 3 "
            "existants : reaper budget, veilleur ASR, suivi-lancement), sans "
            "quoi il réouvre la classe de flake du 2026-10-02.")
        self.assertEqual(
            sans_couture, [],
            "Thread de longue vie dans un fichier SANS couture CRONOS_NO_REAPER : "
            + ", ".join(sans_couture))


class MocksGlobauxRequestsGelTest(unittest.TestCase):
    """No new global mock on the shared `requests` package."""

    MAX_SITES = 48            # effectif gelé au 2026-10-03 (mesuré)

    def test_effectif_des_mocks_globaux_gel(self):
        motif = re.compile(r"patch\.object\(\s*\w+(\.\w+)*\.requests\s*,")
        total = 0
        for f in sorted((RACINE / 'tests').glob('*.py')):
            total += len(motif.findall(f.read_text()))
        self.assertLessEqual(
            total, self.MAX_SITES,
            f"Nouveaux mocks globaux sur `requests` ({total} > {self.MAX_SITES}) : "
            "ils patchent le package partagé par tout le processus. Préférer un "
            "namespace scopé (`_ReqMock` dans test_asr_ondemand.py) ; voir le "
            "flake du 2026-10-02.")


if __name__ == '__main__':
    unittest.main()
