# Whisper (transcription pour la dictée), construit pour le GB10 (Blackwell,
# sm_121) : même base CUDA 13.0 + torch cu130 que les conteneurs voix, seule
# combinaison validée sur cette machine aarch64.
FROM docker.io/nvidia/cuda:13.0.3-runtime-ubuntu22.04

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV DEBIAN_FRONTEND=noninteractive
ENV HF_HOME=/app/hf_cache

RUN apt-get update && apt-get install -y --no-install-recommends \
    libsndfile1 \
    ffmpeg \
    python3 \
    python3-pip \
    ca-certificates \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

RUN ln -s /usr/bin/python3 /usr/bin/python

WORKDIR /app

RUN python3 -m pip install --no-cache-dir --upgrade pip && \
    python3 -m pip install --no-cache-dir \
      --extra-index-url https://download.pytorch.org/whl/cu130 \
      torch==2.10.0 torchaudio==2.10.0

RUN python3 -m pip install --no-cache-dir \
      "transformers>=4.46" accelerate \
      "fastapi>=0.115" "uvicorn[standard]>=0.30" python-multipart \
      soundfile numpy

COPY server.py /app/server.py

RUN mkdir -p /app/hf_cache

EXPOSE 8006

CMD ["python3", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8006"]
