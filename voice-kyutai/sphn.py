"""Stand-in for `sphn` (Kyutai's Rust audio library): it ships no aarch64
wheel and building it needs a Rust toolchain. moshi.models.tts imports it for
ONE call, `sphn.read`, used only when a voice is given as a WAV file — this
service uses precomputed embeddings, so `read` is never reached; it is kept
working (soundfile + linear resampling) rather than left as a trap."""
import numpy as np
import soundfile as sf


def read(path, sample_rate=None):
    data, sr = sf.read(path, dtype="float32", always_2d=True)
    data = data.T
    if sample_rate and sr != sample_rate:
        n = int(round(data.shape[1] * sample_rate / sr))
        x = np.linspace(0, data.shape[1] - 1, n)
        data = np.stack([np.interp(x, np.arange(data.shape[1]), ch) for ch in data]).astype(np.float32)
        sr = sample_rate
    return data, sr
