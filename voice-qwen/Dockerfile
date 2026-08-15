# Qwen3-TTS (Apache 2.0) pour le clonage de voix, construit pour le GB10
# (Blackwell, sm_121) : même base CUDA 13.0 + torch cu130 que le conteneur
# Chatterbox, seule combinaison validée sur cette machine ARM64.
#
# flash-attn n'est volontairement PAS installé : il ne se compile pas
# proprement en aarch64 et server.py retombe sur l'attention native PyTorch
# quand il est absent.
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

# torch d'abord, depuis l'index cu130 : qwen-tts tire sinon une roue CPU/CUDA12
# qui ne connaît pas sm_121.
RUN python3 -m pip install --no-cache-dir --upgrade pip && \
    python3 -m pip install --no-cache-dir \
      --extra-index-url https://download.pytorch.org/whl/cu130 \
      torch==2.10.0 torchaudio==2.10.0

RUN python3 -m pip install --no-cache-dir \
      qwen-tts \
      "fastapi>=0.115" "uvicorn[standard]>=0.30" python-multipart \
      soundfile numpy

COPY server.py /app/server.py

RUN mkdir -p /app/hf_cache

EXPOSE 8005

CMD ["python3", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8005"]
