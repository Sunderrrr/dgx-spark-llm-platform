# -*- coding: utf-8 -*-
"""Anti-regression guards for the patterns that cost us intermittence.

Two families have already failed pushes at random (the 2026-10-02 flake,
three aborted pushes); this file FREEZES their headcount: any addition
makes a test fail, with the explanation of what to do.

1. `threading.Thread(` in the portal: every background thread that calls
   the network during the suite becomes a zombie again (the global
   `requests` mocks catch it at random). THREE exist (budget reaper, ASR
   watcher, launch follower) and all three are behind the
   `DGX_NO_REAPER` seam. A fourth must be too.
2. `patch.object(<module>.requests, …)` in the tests: these mocks patch
   the `requests` package SHARED by the whole process — the exact cause
   of the flake. The existing sites are frozen; any new one should prefer
   a scoped namespace (cf. `_ReqMock` in test_asr_ondemand.py).
"""
import pathlib
import re
import unittest

RACINE = pathlib.Path(__file__).resolve().parent.parent


class BackgroundThreadsFreezeTest(unittest.TestCase):
    """No new long-lived thread without the test seam.

    Measured distinction (2026-10-03): 13 `threading.Thread(` live in the
    portal, but the 10 others are REQUEST-SCOPED (SSE reader, media job
    worker, e-mail sender) — they never leave a request, hence never a
    random test. The real zombies are the NAMED threads: they live from
    one import to the next and talk to the network on their own.
    """

    MAX_NAMED_THREADS = 3   # reaper budget + veilleur ASR + suivi-lancement

    def test_long_lived_threads_count_frozen(self):
        sites, seamless = [], []
        for f in sorted(RACINE.glob('*.py')):
            text = f.read_text()
            for n, line in enumerate(text.splitlines(), 1):
                if 'threading.Thread(' in line and 'name=' in line:
                    sites.append(f"{f.name}:{n}")
                    if 'DGX_NO_REAPER' not in text:
                        seamless.append(f"{f.name}:{n}")
        self.assertLessEqual(
            len(sites), self.MAX_NAMED_THREADS,
            "Nouveau thread de longue vie détecté : " + ", ".join(sites) +
            " — it must be behind `DGX_NO_REAPER=1` (like the 3 "
            "existing ones: budget reaper, ASR watcher, launch follow-up), "
            "otherwise it reopens the 2026-10-02 flake class.")
        self.assertEqual(
            seamless, [],
            "Long-lived thread in a file WITHOUT the DGX_NO_REAPER seam: "
            + ", ".join(seamless))


class GlobalRequestsMocksFreezeTest(unittest.TestCase):
    """No new global mock on the shared `requests` package."""

    MAX_SITES = 48            # count frozen on 2026-10-03 (measured)

    def test_global_mocks_count_frozen(self):
        pattern = re.compile(r"patch\.object\(\s*\w+(\.\w+)*\.requests\s*,")
        total = 0
        for f in sorted((RACINE / 'tests').glob('*.py')):
            total += len(pattern.findall(f.read_text()))
        self.assertLessEqual(
            total, self.MAX_SITES,
            f"New global mocks on `requests` ({total} > {self.MAX_SITES}): "
            "they patch the package shared by the whole process. Prefer a "
            "scoped namespace (`_ReqMock` in test_asr_ondemand.py); see the "
            "2026-10-02 flake.")


if __name__ == '__main__':
    unittest.main()
