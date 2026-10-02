#!/bin/bash
# Recreate the transcription container (Whisper). Same guarantees as
# ocr-recreate.sh / voice-recreate.sh: called by vllm-runner via scoped sudo,
# single argument validated against a closed allowlist, never interpreted by a
# shell. No published port — only dgx-portal reaches it, via asr_net.
#
# $1 = model id (openai/whisper-*)
set -euo pipefail

if [ $# -ne 1 ]; then
  echo "usage: asr-recreate.sh <model_id>" >&2
  exit 2
fi

MODEL="$1"
case "$MODEL" in
  openai/whisper-large-v3-turbo|openai/whisper-large-v3|openai/whisper-medium|openai/whisper-small) ;;
  *) echo "model id invalide : $MODEL" >&2; exit 2 ;;
esac

docker rm -f asr >/dev/null 2>&1 || true

# --security-opt/--cap-drop: aligns this container, which receives raw user
#   bytes, with the hardening of the rest of the platform.
# NO --memory here: on the GB10 the GPU memory is UNIFIED with the RAM and is
#   counted in the container's memory cgroup; an --memory limit would therefore
#   also cap the CUDA allocations and make model loading fail (CUDA out of
#   memory at startup). The anti-decompression-bomb protection is done in the
#   code (header check before decoding, server.py) and by the byte cap, not by
#   the cgroup.
# The HF cache is DEDICATED to ASR (`/root/.cache/huggingface-asr`) and stays
#   writable to allow a cold download of the model at first startup (deploy
#   with no manual step). It therefore no longer mounts the runner's shared
#   `HF_HOME` cache: this container receives raw user bytes and runs as root,
#   so being able to WRITE into the weights the runner will read again at the
#   next launch was a model-poisoning path. The whisper model is copied there
#   once (`cp -a`), with no re-download.
# Log cap identical to the compose services (20 MB x 5): without it the
# container log grows unbounded, and a panicked `docker logs` becomes
# unreadable. Applied at next startup, no downtime here.
exec docker run -d --name asr --restart unless-stopped \
  --log-opt max-size=20m --log-opt max-file=5 \
  --network ai-platform_asr_net --gpus all --shm-size=2g \
  --pids-limit 512 \
  --security-opt no-new-privileges --cap-drop ALL \
  -v /root/.cache/huggingface-asr:/app/hf_cache \
  -e HF_HOME=/app/hf_cache \
  -e ASR_MODEL="$MODEL" \
  whisper-asr:turbo-cu130
