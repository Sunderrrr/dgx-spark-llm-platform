# Kyutai TTS 1.6B (en/fr, CC-BY 4.0) — third engine of the « voice » container,
# the one the playground's voice mode speaks with. Same CUDA 13.0 + torch cu130
# base as the Chatterbox and Qwen3-TTS images (the combination validated on
# the GB10, sm_121).
FROM docker.io/nvidia/cuda:13.0.3-runtime-ubuntu22.04@sha256:ea99f417d171352c988c499da1c4facb89fa166c95bde42cc659cd174f6e7bea

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV DEBIAN_FRONTEND=noninteractive
ENV HF_HOME=/app/hf_cache

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libsndfile1 \
    python3 \
    python3-pip \
    python3-dev \
    ca-certificates \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# torch first, from the cu130 index (sm_121). 2.13 is the version measured with
# this model on the GB10; moshi declares torch<2.10, a conservative pin — it
# is installed --no-deps for that reason, with its real dependencies below.
RUN python3 -m pip install --no-cache-dir --upgrade pip && \
    python3 -m pip install --no-cache-dir \
      --index-url https://download.pytorch.org/whl/cu130 torch==2.13.0

RUN python3 -m pip install --no-cache-dir --no-deps "moshi==0.2.13" && \
    python3 -m pip install --no-cache-dir \
      "numpy<2.3,>=1.26" "safetensors<0.8" "huggingface-hub<1.0" "einops<0.9" \
      "sentencepiece<0.3" "soundfile==0.14.0" \
      "fastapi==0.141.1" "uvicorn[standard]==0.52.1"

# sphn.py stands in for Kyutai's Rust audio library (no aarch64 wheel): see
# the file. It sits next to server.py, on the import path.
COPY sphn.py server.py /app/

RUN mkdir -p /app/hf_cache

EXPOSE 8004

CMD ["python3", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8004"]
