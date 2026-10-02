#!/bin/bash
# Recreate the image generation container (diffusers). Same guarantees as
# ocr-recreate.sh / voice-recreate.sh / asr-recreate.sh: called by vllm-runner
# via scoped sudo, single argument validated against a closed allowlist, never
# interpreted by a shell. No published port — only dgx-portal reaches it, via
# image_net (IMAGE_URL=http://image:8007).
#
# $1 = model id (allowlist below). Each model maps to a diffusers folder
#      already present on the host under /root/models/<slug>: cold download is
#      a separate provisioning step, never triggered from the web.
set -euo pipefail

if [ $# -ne 1 ]; then
  echo "usage: image-recreate.sh <model_id>" >&2
  exit 2
fi

MODEL="$1"
# slug = local folder; NAME = label shown in the UI; STEPS/GUIDANCE =
# inference settings specific to the model. A DISTILLED (few-step) model is
# happy with ~8 steps at guidance 1.0 — pushing it higher oversaturates. A FULL
# model needs 35 to 50 with guidance 4 to 6. No default suits both, hence this
# per-entry setting.
case "$MODEL" in
  black-forest-labs/FLUX.2-klein-4B)
    SLUG="flux2-klein-4b"; NAME="FLUX.2 Klein 4B"
    STEPS=4; GUIDANCE=1.0 ;;
  *) echo "model id invalide : $MODEL" >&2; exit 2 ;;
esac

MODEL_DIR="/root/models/$SLUG"
if [ ! -f "$MODEL_DIR/model_index.json" ]; then
  echo "modèle absent sur l'hôte : $MODEL_DIR (télécharge-le d'abord)" >&2
  exit 3
fi

docker rm -f image >/dev/null 2>&1 || true

# NO --memory: on the GB10 the GPU memory is UNIFIED with the RAM and is
#   counted in the container's memory cgroup; a limit would therefore also cap
#   the CUDA allocations and make loading fail (~16 GB for FLUX.2 Klein 4B).
# --cap-drop/--security-opt: this container receives a user prompt into
#   third-party model code, same hardening as the other sidecars.
# Model mounted read-only; only dgx-portal reaches port 8007 (image_net).
# Log cap identical to the compose services (20 MB x 5): without it the
# container log grows unbounded, and a panicked `docker logs` becomes
# unreadable. Applied at next startup, no downtime here.
exec docker run -d --name image --restart unless-stopped \
  --log-opt max-size=20m --log-opt max-file=5 \
  --network ai-platform_image_net --gpus all --shm-size=2g \
  --pids-limit 512 \
  --security-opt no-new-privileges --cap-drop ALL \
  -v "$MODEL_DIR":/model:ro \
  -v /root/models/esrgan:/esrgan:ro \
  -e MODEL_DIR=/model -e MODEL_NAME="$NAME" \
  -e IMAGE_STEPS="$STEPS" -e IMAGE_GUIDANCE="$GUIDANCE" \
  ai-platform-image-gen
