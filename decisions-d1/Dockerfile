# Decision model: LiquidAI/d1-3B served behind the SAME System One contract as
# the previous engine (`POST /v1/systemone`: a state plus typed questions,
# calibrated answers out). Replaces the Clef GGUF, which llama.cpp served on
# :8002.
#
# The model ships its own code (`modeling_d1.py`, `api.py`, `prompt.py`), so it
# is loaded with trust_remote_code — acceptable here: the repository is pinned by
# revision and the weights are read-only.
FROM docker.io/nvidia/cuda:13.0.3-runtime-ubuntu24.04@sha256:760753ab2971b5c4b5ba8e055334e3c1126c3d88597fc702623eae402257e737

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DEBIAN_FRONTEND=noninteractive
ENV HF_HOME=/app/hf_cache

RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-pip python3-dev build-essential git libsndfile1 ca-certificates \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# torch from the cu130 index (the GB10 is sm_121), then the model's own minimum.
RUN python3 -m pip install --no-cache-dir --break-system-packages \
      --extra-index-url https://download.pytorch.org/whl/cu130 torch torchvision
RUN python3 -m pip install --no-cache-dir --break-system-packages \
      "transformers>=5.14" "pillow" "fastapi" "uvicorn[standard]" "accelerate" "safetensors" "huggingface_hub"

WORKDIR /app
COPY server.py /app/server.py
ENV D1_MODEL=/app/model
EXPOSE 8002
CMD ["python3", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8002"]
