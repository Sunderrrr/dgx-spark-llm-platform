# Qwen3-TTS (Apache 2.0) for voice cloning, built for the GB10
# (Blackwell, sm_121): same CUDA 13.0 + torch cu130 base as the Chatterbox
# container, the only combination validated on this ARM64 machine.
#
# flash-attn is deliberately NOT installed: it does not compile cleanly on
# aarch64 and server.py falls back to native PyTorch attention when absent.
FROM docker.io/nvidia/cuda:13.0.3-runtime-ubuntu22.04@sha256:ea99f417d171352c988c499da1c4facb89fa166c95bde42cc659cd174f6e7bea

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV DEBIAN_FRONTEND=noninteractive
ENV HF_HOME=/app/hf_cache

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libsndfile1 \
    ffmpeg \
    python3 \
    python3-pip \
    python3-dev \
    git \
    ca-certificates \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

RUN ln -s /usr/bin/python3 /usr/bin/python

WORKDIR /app

# torch first, from the cu130 index: otherwise qwen-tts pulls a CPU/CUDA12
# wheel that does not know sm_121.
RUN python3 -m pip install --no-cache-dir --upgrade pip && \
    python3 -m pip install --no-cache-dir \
      --extra-index-url https://download.pytorch.org/whl/cu130 \
      torch==2.10.0 torchaudio==2.10.0

RUN python3 -m pip install --no-cache-dir \
      qwen-tts \
      "fastapi==0.141.1" "uvicorn[standard]==0.52.1" python-multipart \
      "soundfile==0.14.0" "numpy==2.2.6"

COPY server.py /app/server.py

RUN mkdir -p /app/hf_cache

# 8004, like the Chatterbox sidecar: the port is FIXED on the platform side
# (`VOICE_URL=http://voice:8004` in config.py, and the two engines are
# substitutes for each other). This CMD is never the one running anyway:
# `voice-qwen-recreate.sh` starts the container with its own arguments,
# including `--port 8004`. Advertising 8005 here only sowed doubt.
EXPOSE 8004

CMD ["python3", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8004"]
