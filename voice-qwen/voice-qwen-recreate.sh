#!/bin/bash
# Recreate the voice container with Qwen3-TTS. Same principle and same
# guarantees as voice-recreate.sh (Chatterbox): called by vllm-runner via
# scoped sudo, single argument validated against a closed allowlist, never
# interpreted by a shell.
#
# Both engines share the container name « voice » and the voice_net network:
# only one voice backend runs at a time, which is intended on an already loaded
# unified-memory machine (chat + OCR + video).
#
# $1 = model id (Qwen3-TTS-12Hz-1.7B-Base | Qwen3-TTS-12Hz-0.6B-Base)
set -euo pipefail

if [ $# -ne 1 ]; then
  echo "usage: voice-qwen-recreate.sh <model_id>" >&2
  exit 2
fi

MODEL="$1"
case "$MODEL" in
  Qwen3-TTS-12Hz-1.7B-Base|Qwen3-TTS-12Hz-0.6B-Base) ;;
  *) echo "model id invalide : $MODEL" >&2; exit 2 ;;
esac

docker rm -f voice >/dev/null 2>&1 || true

# No published port (like the OCR container and the Chatterbox container):
# only dgx-portal reaches it, via the voice_net network.
#
# The port is forced to 8004, the one Chatterbox listens on, so that VOICE_URL
# stays identical whatever the engine: on the portal side, only the `engine`
# field of /api/model-info tells the two apart.
# Hardening: see asr-recreate.sh. This container too decodes a user-provided
# audio file; same guards (header check in the code). NO --memory: GB10 unified
# memory, see asr-recreate.sh.
# Log cap identical to the compose services (20 MB x 5): without it the
# container log grows unbounded, and a panicked `docker logs` becomes
# unreadable. Applied at next startup, no downtime here.
exec docker run -d --name voice --restart unless-stopped \
  --log-opt max-size=20m --log-opt max-file=5 \
  --network ai-platform_voice_net --gpus all --shm-size=4g \
  --pids-limit 512 \
  --security-opt no-new-privileges --cap-drop ALL \
  -v /root/.cache/huggingface:/app/hf_cache \
  -e HF_HOME=/app/hf_cache \
  -e QWEN_TTS_MODEL="Qwen/$MODEL" \
  qwen3-tts-voice:1.7b-cu130 \
  python3 -m uvicorn server:app --host 0.0.0.0 --port 8004
