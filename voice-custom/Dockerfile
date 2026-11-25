# Personal voice: Fish Audio S2 Pro (weights: Fish Audio Research License,
# personal / non-commercial use) — fourth engine of the « voice » container,
# the one that speaks the voice mode with the operator's own voice. Same
# CUDA 13.0 + torch cu130 base as the other voice engines (validated on the
# GB10, sm_121, aarch64).
#
# fish-speech pins torch 2.8, which has no build for sm_121: the inference
# code is installed WITHOUT its pins, on torch 2.10, with the dependency set
# below (resolved and import-tested against this exact commit). Source cloned
# at build time from a pinned commit, like the Chatterbox image.
#
# Ubuntu 24.04 for its Python 3.12: librosa 1.0.1 and ormsgpack 1.13.1 both
# require >= 3.11, and the 22.04 base ships 3.10 — the build failed there
# (measured 2026-11-25). 24.04 marks its Python externally managed (PEP 668),
# hence --break-system-packages; the image is a single-purpose container.
FROM docker.io/nvidia/cuda:13.0.3-runtime-ubuntu24.04@sha256:760753ab2971b5c4b5ba8e055334e3c1126c3d88597fc702623eae402257e737

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV DEBIAN_FRONTEND=noninteractive
ENV HF_HOME=/app/hf_cache

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libsndfile1 \
    ffmpeg \
    git \
    python3 \
    python3-pip \
    python3-dev \
    ca-certificates \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# No `pip --upgrade` here: 24.04's pip is Debian-managed (no RECORD), so pip
# cannot uninstall it and refuses. Its pip 24.0 resolves the pins below.
RUN python3 -m pip install --no-cache-dir --break-system-packages \
      --extra-index-url https://download.pytorch.org/whl/cu130 \
      torch==2.10.0 torchaudio==2.10.0

# fish-speech at the commit this engine was written against (supports S2 Pro).
ARG FISH_SPEECH_REF=0f8a12e
RUN git clone https://github.com/fishaudio/fish-speech.git /opt/fish-speech && \
    git -C /opt/fish-speech checkout ${FISH_SPEECH_REF} && \
    rm -rf /opt/fish-speech/.git

RUN python3 -m pip install --no-cache-dir --break-system-packages \
      "transformers==5.19.0" "tokenizers==0.23.2" "hydra-core==1.3.8" "natsort==8.4.0" \
      "einops==0.8.2" "einx[torch]==0.2.2" "librosa==1.0.1" "resampy==0.4.3" "soundfile==0.14.0" \
      "loguru==0.8.0" "pyrootutils==1.0.4" "pydub==0.25.1" "opencc-python-reimplemented==0.1.7" \
      "ormsgpack==1.13.1" "tiktoken==0.14.0" "pydantic==2.9.2" "cachetools==8.0.2" \
      "safetensors==0.8.0" "kui==1.15.0" "huggingface_hub==1.33.0" \
      "tensorboard==2.21.0" "flatten-dict==0.6.0" "julius==0.2.8" "ffmpy==1.0.0" \
      "importlib_resources==7.1.0" "pyloudnorm==0.2.0" "markdown2==2.5.5" "rich==15.0.0" \
      "randomname==0.2.1" "argbind==0.3.9" \
      "fastapi==0.141.1" "uvicorn[standard]==0.52.1" && \
    # The DAC codec packages pin an old torch/protobuf they do not need at
    # inference: installed without their dependencies.
    python3 -m pip install --no-cache-dir --break-system-packages --no-deps \
      "descript-audio-codec==1.0.0" "descript-audiotools==0.7.2"

# torchaudio 2.10 decodes ONLY through TorchCodec, whatever `backend=` is passed:
# its wheel does not load against this torch ABI (libtorchcodec_image.so), and
# without it fish-speech cannot even read the reference clip. The one call that
# matters is replaced with libsndfile (installed), which reads these PCM wavs.
RUN python3 - <<'PY'
from pathlib import Path
p = Path("/opt/fish-speech/fish_speech/inference_engine/reference_loader.py")
s = p.read_text()
old = "waveform, original_sr = torchaudio.load(reference_audio, backend=self.backend)"
new = (
    "import soundfile as _sf\n"
    "        import torch as _torch\n"
    "        if hasattr(reference_audio, \"read\"):\n"
    "            try:\n"
    "                reference_audio.seek(0)\n"
    "            except (OSError, ValueError):\n"
    "                pass\n"
    "            _data, original_sr = _sf.read(reference_audio, dtype=\"float32\", always_2d=True)\n"
    "        else:\n"
    "            _data, original_sr = _sf.read(str(reference_audio), dtype=\"float32\", always_2d=True)\n"
    "        waveform = _torch.from_numpy(_data.T.copy())"
)
assert old in s, "reference_loader: appel introuvable"
p.write_text(s.replace(old, new))
print("reference_loader patched")
PY

WORKDIR /app
COPY s2engine.py server.py /app/
COPY reference/ /app/reference/
ENV PYTHONPATH=/opt/fish-speech:/app
ENV FISH_SPEECH_DIR=/opt/fish-speech
RUN mkdir -p /app/hf_cache /app/references

EXPOSE 8004

CMD ["python3", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8004"]
