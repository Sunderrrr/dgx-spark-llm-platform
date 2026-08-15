# Krea-2 (text-to-image) sidecar, built for the GB10 (Blackwell, sm_121): same
# CUDA 13.0 + torch cu130 base as the voice/ASR containers — the only combo
# validated on this aarch64 box. Krea-2 needs diffusers from git (Krea2Pipeline).
FROM docker.io/nvidia/cuda:13.0.3-runtime-ubuntu22.04@sha256:ea99f417d171352c988c499da1c4facb89fa166c95bde42cc659cd174f6e7bea

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV DEBIAN_FRONTEND=noninteractive
ENV HF_HOME=/app/hf_cache

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip ca-certificates git \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

RUN ln -s /usr/bin/python3 /usr/bin/python

WORKDIR /app

RUN python3 -m pip install --no-cache-dir --upgrade pip && \
    python3 -m pip install --no-cache-dir \
      --extra-index-url https://download.pytorch.org/whl/cu130 \
      torch==2.10.0

# Krea2Pipeline lives in diffusers main (not a release yet).
RUN python3 -m pip install --no-cache-dir \
      "git+https://github.com/huggingface/diffusers.git" \
      "transformers>=4.46" accelerate safetensors sentencepiece protobuf Pillow \
      "fastapi>=0.115" "uvicorn[standard]>=0.30" python-multipart

COPY server.py /app/server.py

RUN mkdir -p /app/hf_cache

EXPOSE 8007

CMD ["python3", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8007"]
