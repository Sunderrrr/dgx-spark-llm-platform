#!/bin/bash
# Recreate the voice container with the personal voice (Fish Audio S2 Pro).
# Same principle and guarantees as the other voice engines: called by
# vllm-runner via scoped sudo, single argument checked against a closed
# allowlist, never interpreted by a shell. The engines share the container
# name « voice », the voice_net network and port 8004: one backend at a time.
#
# $1 = repo id (custom-voice)
#
# The voice is whatever install-voice.sh last activated:
# /var/lib/voice-custom/active → /var/lib/voice-custom/<name> (ref.wav + ref.lab).
# The symlink is resolved HERE (root-owned path, no argument involved) and the
# real directory mounted read-only as the engine's only reference.
set -euo pipefail

if [ $# -ne 1 ]; then
  echo "usage: voice-custom-recreate.sh <repo_id>" >&2
  exit 2
fi

case "$1" in
  custom-voice) ;;
  *) echo "repo id invalide : $1" >&2; exit 2 ;;
esac

ROOT=/var/lib/voice-custom
if [ ! -L "$ROOT/active" ]; then
  echo "aucune voix personnalisée installée (voice-custom/install-voice.sh)" >&2
  exit 1
fi
VOICE=$(readlink -f "$ROOT/active")
case "$VOICE" in
  "$ROOT"/*) ;;
  *) echo "lien active hors de $ROOT : $VOICE" >&2; exit 1 ;;
esac
if [ ! -f "$VOICE/ref.wav" ] || [ ! -f "$VOICE/ref.lab" ]; then
  echo "voix incomplète : $VOICE" >&2
  exit 1
fi

docker rm -f voice >/dev/null 2>&1 || true

# The weights (~11 GB, public) come through the host HF cache like Kyutai's;
# HF_TOKEN_PATH points away from the host token file (unreadable without
# CAP_DAC_OVERRIDE, see voice-kyutai-recreate.sh). No published port: only
# dgx-portal reaches it, via voice_net. NO --memory: on GB10 unified memory a
# container cap also caps GPU memory and breaks CUDA loading.
exec docker run -d --name voice --restart unless-stopped \
  --log-opt max-size=20m --log-opt max-file=5 \
  --network ai-platform_voice_net --gpus all --shm-size=4g \
  --pids-limit 512 \
  --security-opt no-new-privileges --cap-drop ALL \
  -v "$VOICE:/app/references/voice:ro" \
  -v "${HOME}/.cache/huggingface:/app/hf_cache" \
  -e HF_HOME=/app/hf_cache \
  -e HF_TOKEN_PATH=/app/no-hf-token \
  fish-s2pro-voice:0f8a12e-cu130 \
  python3 -m uvicorn server:app --host 0.0.0.0 --port 8004
