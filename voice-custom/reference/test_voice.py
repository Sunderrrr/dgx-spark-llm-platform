"""Listen before choosing: the same sentences with every candidate reference.

Each subdirectory of --refs holding ref.wav + ref.lab is a candidate. S2 Pro
is loaded ONCE (exactly as server.py loads it) and every candidate speaks
the same sentences, written to <out>/<candidate>/<nn>.wav. Printed per
candidate: the measured speed on this machine (RTF < 1 = faster than speech)
and the time to the first sentence — the numbers that decide whether the voice
mode stays conversational.
"""
import argparse
import csv
import os
import sys
import tempfile
import time
from pathlib import Path

import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import s2engine  # noqa: E402

SENTENCES = [
    "Salut, qu'est-ce que je peux faire pour toi aujourd'hui ?",
    "D'accord, je regarde ça tout de suite.",
    "Le serveur a redémarré cette nuit à trois heures, et la sauvegarde s'est terminée sans erreur.",
    "Honnêtement, je pense que la deuxième solution est la plus simple.",
    "Attends, tu es sûr ? Ça me paraît un peu risqué.",
    "Super nouvelle, félicitations !",
    "Il reste soixante-quinze pour cent de batterie et environ deux heures d'autonomie.",
    "Le cluster Proxmox tourne sur trois nœuds, avec un stockage Ceph partagé.",
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--refs", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--checkpoint", default=os.environ.get("S2PRO_CHECKPOINT", "fishaudio/s2-pro"))
    ap.add_argument("--no-compile", action="store_true")
    a = ap.parse_args()

    cands = sorted(p for p in a.refs.iterdir() if (p / "ref.wav").exists() and (p / "ref.lab").exists())
    if not cands:
        sys.exit(f"no candidate (ref.wav + ref.lab) under {a.refs}")
    work = Path(tempfile.mkdtemp())
    (work / "references").mkdir()
    for c in cands:
        (work / "references" / c.name).symlink_to(c.resolve())
    os.chdir(work)               # fish-speech reads ./references/<id>/

    voice = s2engine.load(s2engine.resolve_checkpoint(a.checkpoint), not a.no_compile)
    print(f"S2 Pro loaded in {voice.load_s:.0f} s (compile={voice.compiled})", flush=True)
    rows = []
    for c in cands:
        dst = a.out / c.name
        dst.mkdir(parents=True, exist_ok=True)
        voice.synth("Bonjour.", c.name)          # encodes and caches the reference
        gen_s = audio_s = 0.0
        first = None
        for i, text in enumerate(SENTENCES, 1):
            t = time.time()
            audio = voice.synth(text, c.name)
            dt = time.time() - t
            first = dt if first is None else first
            sf.write(dst / f"{i:02d}.wav", audio, voice.sample_rate)
            gen_s += dt
            audio_s += len(audio) / voice.sample_rate
            rows.append({"candidate": c.name, "n": i, "text": text,
                         "audio_s": round(len(audio) / voice.sample_rate, 2), "gen_s": round(dt, 2)})
        print(f"{c.name:>12}: RTF {gen_s / audio_s:.2f}, first sentence in {first:.1f} s", flush=True)
    with open(a.out / "summary.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"samples in {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
