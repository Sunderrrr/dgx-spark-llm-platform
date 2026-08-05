# Chatterbox TTS Server (devnen/Chatterbox-TTS-Server, MIT), built for the
# DGX Spark's GB10 (Blackwell, sm_121) using the project's own CUDA 13.0
# variant — the README explicitly documents DGX Spark support via this exact
# base image + requirements file. Source isn't vendored here: cloned at build
# time from the pinned release tag below.
FROM docker.io/nvidia/cuda:13.0.3-runtime-ubuntu22.04

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
    python3-venv \
    git \
    ca-certificates \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

RUN ln -s /usr/bin/python3 /usr/bin/python

WORKDIR /app

# Pinned release, not a moving branch — reproducible builds.
ARG CHATTERBOX_SERVER_REF=v2.0.0
RUN git clone --branch ${CHATTERBOX_SERVER_REF} --depth 1 \
    https://github.com/devnen/Chatterbox-TTS-Server.git .

RUN python3 -m pip install --no-cache-dir --upgrade pip && \
    python3 -m pip install --no-cache-dir -r requirements-nvidia-cu130.txt && \
    python3 -m pip install --no-cache-dir --no-deps \
      git+https://github.com/devnen/chatterbox-v2.git@master s3tokenizer==0.3.0 onnx==1.16.0 && \
    python3 -m pip install --no-cache-dir "protobuf>=4.25.0"

RUN mkdir -p model_cache reference_audio outputs voices logs hf_cache

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 8004

CMD ["/usr/local/bin/entrypoint.sh"]
