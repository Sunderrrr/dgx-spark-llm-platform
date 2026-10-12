#!/bin/bash
# Recreate the voice container with Kyutai TTS. Same principle and guarantees
# as voice-recreate.sh / voice-qwen-recreate.sh: called by vllm-runner via
# scoped sudo, single argument checked against a closed allowlist, never
# interpreted by a shell. The three engines share the container name « voice »,
# the voice_net network and port 8004: one voice backend at a time.
#
# $1 = repo id (kyutai/tts-1.6b-en_fr)
set -euo pipefail

if [ $# -ne 1 ]; then
  echo "usage: voice-kyutai-recreate.sh <repo_id>" >&2
  exit 2
fi

case "$1" in
  kyutai/tts-1.6b-en_fr) ;;
  *) echo "repo id invalide : $1" >&2; exit 2 ;;
esac

docker rm -f voice >/dev/null 2>&1 || true

# The model and voices are public: the host's HF token file (0600, owned by
# vllmrunner) is unreadable without CAP_DAC_OVERRIDE, and huggingface_hub
# reading it implicitly crashed the load — even with
# HF_HUB_DISABLE_IMPLICIT_TOKEN it is read; HF_TOKEN_PATH points elsewhere.
# No published port: only dgx-portal reaches it, via voice_net. Hardening and
# log cap as for the other voice engines. NO --memory: on GB10 unified memory
# a container cap also caps GPU memory and breaks CUDA loading.
exec docker run -d --name voice --restart unless-stopped \
  --log-opt max-size=20m --log-opt max-file=5 \
  --network ai-platform_voice_net --gpus all --shm-size=2g \
  --pids-limit 512 \
  --security-opt no-new-privileges --cap-drop ALL \
  -v /root/.cache/huggingface:/app/hf_cache \
  -e HF_HOME=/app/hf_cache \
  -e HF_TOKEN_PATH=/app/no-hf-token \
  -e KYUTAI_TTS_REPO="$1" \
  kyutai-tts-voice:1.6b-cu130 \
  python3 -m uvicorn server:app --host 0.0.0.0 --port 8004
