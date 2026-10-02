# Text-to-image sidecar (diffusers), built for the GB10 (Blackwell,
# sm_121): same CUDA 13.0 + torch cu130 base as the voice/dictation containers —
# the only combination validated on this aarch64 machine. Recent pipelines
# (Cosmos3OmniPipeline among others) are not yet in a published diffusers
# release, hence the install from git.
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

# diffusers from git: the pipeline classes used here (Cosmos3Omni among
# others) do not exist yet in a published release. Pinned commit (the one
# observed in production via direct_url.json) — do NOT go back to HEAD: a
# moving commit brings unaudited code into the sidecar at every build.
# bitsandbytes: MANDATORY for NF4 (4-bit) pre-quantized models, otherwise
#   from_pretrained fails on the quantization_config embedded in the repo.
#   aarch64 wheel available from 0.50 (manylinux_2_24_aarch64).
RUN python3 -m pip install --no-cache-dir \
      "git+https://github.com/huggingface/diffusers.git@efabd60d61c2b7aabf9f182bee6b5b6058980304" \
      "transformers>=4.46" "accelerate==1.14.0" safetensors sentencepiece protobuf Pillow \
      "numpy==2.2.6" \
      "bitsandbytes>=0.50" \
      "fastapi==0.141.1" "uvicorn[standard]==0.52.1" python-multipart

COPY server.py /app/server.py
COPY esrgan.py /app/esrgan.py

RUN mkdir -p /app/hf_cache

EXPOSE 8007

CMD ["python3", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8007"]
