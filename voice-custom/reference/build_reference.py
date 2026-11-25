"""Build one reference voice (ref.wav + ref.lab) from a recorded dataset.

S2 Pro clones a voice in context from a clip and its EXACT transcript. The
clip should be natural speech in the register the voice will be used for
(conversation), clean, 10–30 s. This joins dataset clips with short pauses,
evens the level, and writes the transcript the model must see.

  <dataset>/wavs/<name>.wav + <dataset>/metadata.csv (`<name>.wav|<text>`)
  →  <out>/ref.wav (44.1 kHz mono PCM16)  <out>/ref.lab
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

SR = 44100
GAP_S = 0.25
MIN_S, MAX_S = 5.0, 40.0
TARGET_RMS_DB = -20.0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", required=True, type=Path)
    ap.add_argument("--clips", required=True, help="comma-separated wav names, in speaking order")
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args()

    import librosa
    texts = {}
    for line in open(a.dataset / "metadata.csv", encoding="utf-8"):
        if "|" in line:
            name, text = line.rstrip("\n").split("|", 1)
            texts[name.strip()] = text.strip()
    names = [n.strip() for n in a.clips.split(",") if n.strip()]
    parts, lab = [], []
    gap = np.zeros(int(GAP_S * SR), dtype=np.float32)
    for i, n in enumerate(names):
        if n not in texts:
            sys.exit(f"{n}: not in metadata.csv (excluded or misspelt)")
        audio, _ = librosa.load(str(a.dataset / "wavs" / n), sr=SR, mono=True)
        if i:
            parts.append(gap)
        parts.append(audio.astype(np.float32))
        lab.append(texts[n])
    ref = np.concatenate(parts)
    dur = len(ref) / SR
    if not MIN_S <= dur <= MAX_S:
        sys.exit(f"reference is {dur:.1f} s, keep it within {MIN_S:.0f}–{MAX_S:.0f} s")
    rms = float(np.sqrt(np.mean(ref ** 2))) or 1e-9
    ref = ref * (10 ** (TARGET_RMS_DB / 20) / rms)
    peak = float(np.abs(ref).max())
    if peak > 0.89:                     # keep ~1 dB of headroom
        ref = ref * (0.89 / peak)
    a.out.mkdir(parents=True, exist_ok=True)
    sf.write(a.out / "ref.wav", ref, SR, subtype="PCM_16")
    (a.out / "ref.lab").write_text(" ".join(lab) + "\n", encoding="utf-8")
    print(f"{a.out}: {dur:.1f} s, {len(names)} clips")
    return 0


if __name__ == "__main__":
    sys.exit(main())
