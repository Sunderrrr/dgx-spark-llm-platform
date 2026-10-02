#!/bin/bash
# Recreate the music generation container ("music") with another HF model.
# Same guarantees as ocr-recreate.sh: called by vllm-runner via scoped sudo,
# single argument validated on the Python side ("org/name" shape) then passed
# in argv, never interpreted by a shell. No published port — only dgx-portal
# reaches it via music_net.
#
# $1 = model id HuggingFace (ex: MiniMaxAI/MiniMax-Music3)
set -euo pipefail

if [ $# -ne 1 ]; then
  echo "usage: music-recreate.sh <hf_model_id>" >&2
  exit 2
fi

MODEL="$1"
# Shape guard, on top of the Python validation: only "org/name".
case "$MODEL" in
  */*) : ;;
  *) echo "model id invalide : $MODEL" >&2; exit 2 ;;
esac
case "$MODEL" in
  *..*|*" "*|-*) echo "model id invalide : $MODEL" >&2; exit 2 ;;
esac

docker rm -f music >/dev/null 2>&1 || true

# NO --memory: GB10 unified memory, a limit would cap the VRAM too and make
#   CUDA loading fail (see CLAUDE.md).
# --cap-drop/--security-opt: this container runs third-party model code
#   downloaded from HF, same hardening as the other sidecars.
# HF cache writable: the model downloads itself at first startup, which allows
#   adding a model from the admin without going through the shell.
# Log cap identical to the compose services (20 MB x 5): without it the
# container log grows unbounded, and a panicked `docker logs` becomes
# unreadable. Applied at next startup, no downtime here.
exec docker run -d --name music --restart unless-stopped \
  --log-opt max-size=20m --log-opt max-file=5 \
  --network ai-platform_music_net --gpus all --shm-size=2g \
  --pids-limit 512 \
  --security-opt no-new-privileges --cap-drop ALL \
  -v /root/.cache/huggingface-music:/app/hf_cache \
  -e HF_HOME=/app/hf_cache \
  -e MUSIC_MODEL="$MODEL" \
  -e MUSIC_QUANT="${MUSIC_QUANT:-8bit}" \
  ai-platform-music
