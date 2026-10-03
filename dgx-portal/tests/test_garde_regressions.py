# -*- coding: utf-8 -*-
"""Garde-fous anti-régression des motifs qui ont coûté de l'intermittence.

Deux familles ont déjà fait échouer des pushes au hasard (flake du
2026-10-02, trois pushes avortés) ; ce fichier GÈLE leurs effectifs : tout
ajout fait tomber un test, avec l'explication de ce qu'il faut faire.

1. `threading.Thread(` dans le portail : chaque fil d'arrière-plan qui
   appelle le réseau pendant la suite redevient un zombie (les mocks
   globaux de `requests` l'attrapent au hasard). TROIS existent (reaper
   budget, veilleur ASR, suivi-lancement) et les trois sont derrière la
   couture `CRONOS_NO_REAPER`. Un quatrième doit l'être aussi.
2. `patch.object(<module>.requests, …)` dans les tests : ces mocks
   patchent le package `requests` PARTAGÉ par tout le processus — le
   motif exact du flake. Les sites existants sont gelés ; tout nouveau
   doit préférer un namespace scopé (cf. `_ReqMock` dans
   test_asr_ondemand.py).
"""
import pathlib
import re
import unittest

RACINE = pathlib.Path(__file__).resolve().parent.parent


class ThreadsArrierePlanGelTest(unittest.TestCase):
    """Aucun nouveau thread de longue vie sans la couture de test.

    Distinction mesurée (2026-10-03) : 13 `threading.Thread(` vivent dans le
    portail, mais les 10 autres sont SCOPÉS REQUÊTE (lecteur SSE, worker de
    job média, envoi d'e-mail) — ils ne sortent jamais d'une requête, donc
    jamais d'un test au hasard. Les vrais zombies sont les threads NOMMÉS :
    ils vivent d'un import à l'autre et parlent au réseau seuls.
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
    """Aucun nouveau mock global sur le package `requests` partagé."""

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
