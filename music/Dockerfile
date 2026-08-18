# MiniMax-Music3 (text-to-music) et autres modèles musique diffusers, pour le
# GB10 (Blackwell, sm_121) : même base CUDA 13.0 + torch cu130 que les sidecars
# image/voix/ASR — la seule pile validée sur cet aarch64.
FROM docker.io/nvidia/cuda:13.0.3-runtime-ubuntu22.04@sha256:ea99f417d171352c988c499da1c4facb89fa166c95bde42cc659cd174f6e7bea

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV DEBIAN_FRONTEND=noninteractive
ENV HF_HOME=/app/hf_cache

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip ca-certificates git libsndfile1 \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN ln -s /usr/bin/python3 /usr/bin/python

WORKDIR /app

RUN python3 -m pip install --no-cache-dir --upgrade pip && \
    python3 -m pip install --no-cache-dir \
      --extra-index-url https://download.pytorch.org/whl/cu130 \
      torch==2.10.0

# MiniMax-Music3 vit dans une PR diffusers non encore fusionnée : on épingle le
# commit exact recommandé par la fiche du modèle (reproductible).
RUN python3 -m pip install --no-cache-dir \
      "git+https://github.com/huggingface/diffusers@dafe3733fcfdbf3c48915fe77be3aef65b5d6a2d" \
      "transformers>=4.46" accelerate safetensors sentencepiece protobuf \
      soundfile numpy \
      "fastapi>=0.115" "uvicorn[standard]>=0.30" python-multipart \
      bitsandbytes

COPY server.py /app/server.py
RUN mkdir -p /app/hf_cache

EXPOSE 8008
CMD ["python3", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8008"]
