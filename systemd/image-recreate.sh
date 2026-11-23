#!/bin/bash
# Recreate the image generation container (diffusers). Same guarantees as
# ocr-recreate.sh / voice-recreate.sh / asr-recreate.sh: called by vllm-runner
# via scoped sudo; every argument is validated by SHAPE, never interpreted by a
# shell. No published port — only dgx-portal reaches it, via image_net
# (IMAGE_URL=http://image:8007).
#
# $1 = Hugging Face model id, $2 = local slug (${HOME}/models/<slug>), $3 = label,
# $4 = inference steps, $5 = guidance. The catalog (image_configs) carries all
# five: a distilled few-step model needs ~4 steps at guidance 1.0, a full one 28
# to 50 at guidance 4 to 6, and no default suits both. Weights are provisioned on
# the host separately, never downloaded from the web.
set -euo pipefail

if [ $# -ne 5 ]; then
  echo "usage: image-recreate.sh <model_id> <slug> <name> <steps> <guidance>" >&2
  exit 2
fi

MODEL="$1"
SLUG="$2"
NAME="$3"
STEPS="$4"
GUIDANCE="$5"

# Every argument is validated by SHAPE, not against a closed list: the models
# live in image_configs (the admin catalog), and the operator adds one at a time.
# The folder must already hold a diffusers checkpoint — the download is a separate
# provisioning step, never triggered from the web.
case "$MODEL" in
  */*) ;;
  *) echo "invalid model id: $MODEL" >&2; exit 2 ;;
esac
case "$MODEL" in
  *[!A-Za-z0-9._/-]*) echo "invalid model id: $MODEL" >&2; exit 2 ;;
esac
case "$SLUG" in
  [a-z0-9]*) ;;
  *) echo "invalid slug: $SLUG" >&2; exit 2 ;;
esac
case "$SLUG" in
  *[!a-z0-9-]*) echo "invalid slug: $SLUG" >&2; exit 2 ;;
esac
[ ${#SLUG} -le 60 ] || { echo "slug too long" >&2; exit 2; }
case "$STEPS" in
  ''|*[!0-9]*) echo "invalid steps: $STEPS" >&2; exit 2 ;;
esac
[ "$STEPS" -ge 1 ] && [ "$STEPS" -le 200 ] || { echo "steps out of range: $STEPS" >&2; exit 2; }
LC_ALL=C printf '%s' "$GUIDANCE" | grep -qE '^[0-9]+([.][0-9]+)?$' || { echo "invalid guidance: $GUIDANCE" >&2; exit 2; }
awk -v g="$GUIDANCE" 'BEGIN { exit !(g >= 0 && g <= 30) }' || { echo "guidance out of range: $GUIDANCE" >&2; exit 2; }

MODEL_DIR="${HOME}/models/$SLUG"
if [ ! -f "$MODEL_DIR/model_index.json" ]; then
  echo "model missing on the host: $MODEL_DIR (download it first)" >&2
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
  -v "${HOME}/models/esrgan:/esrgan:ro" \
  -e MODEL_DIR=/model -e MODEL_NAME="$NAME" \
  -e IMAGE_STEPS="$STEPS" -e IMAGE_GUIDANCE="$GUIDANCE" \
  ai-platform-image-gen
