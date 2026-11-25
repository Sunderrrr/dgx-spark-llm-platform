#!/bin/bash
# Recreate the voice container with the FINE-TUNED Qwen3-TTS (the operator's own
# voice). Same guarantees as voice-qwen-recreate.sh: called by vllm-runner via
# scoped sudo, single argument from a closed allowlist, never interpreted by a
# shell.
#
# $1 = repo id (custom-qwen-tts)
#
# The checkpoint is the one the official recipe wrote
# (QwenLM/Qwen3-TTS/finetuning/sft_12hz.py, single-speaker): it lives under
# /var/lib/qwen-tts-finetuned/<speaker> and speaks with the speaker name it was
# trained with — no reference clip at inference. The link `active` selects it.
set -euo pipefail

if [ $# -ne 1 ]; then
  echo "usage: voice-qwen-custom-recreate.sh <repo_id>" >&2
  exit 2
fi
case "$1" in
  custom-qwen-tts) ;;
  *) echo "repo id invalide : $1" >&2; exit 2 ;;
esac

ROOT=/var/lib/qwen-tts-finetuned
if [ ! -L "$ROOT/active" ]; then
  echo "aucune voix fine-tunée installée dans $ROOT" >&2
  exit 1
fi
VOICE=$(readlink -f "$ROOT/active")
case "$VOICE" in
  "$ROOT"/*) ;;
  *) echo "lien active hors de $ROOT : $VOICE" >&2; exit 1 ;;
esac
if [ ! -f "$VOICE/model.safetensors" ] || [ ! -f "$VOICE/config.json" ]; then
  echo "checkpoint incomplet : $VOICE" >&2
  exit 1
fi
SPEAKER=$(basename "$VOICE")

docker rm -f voice >/dev/null 2>&1 || true

# No published port: only the portal reaches it, via voice_net, on 8004 (the
# port every voice engine uses). NO --memory: on GB10 unified memory a
# container cap also caps GPU memory and breaks CUDA loading.
exec docker run -d --name voice --restart unless-stopped \
  --log-opt max-size=20m --log-opt max-file=5 \
  --network ai-platform_voice_net --gpus all --shm-size=4g \
  --pids-limit 512 \
  --security-opt no-new-privileges --cap-drop ALL \
  -v "${HOME}/.cache/huggingface:/app/hf_cache" \
  -v "$VOICE:/app/finetune:ro" \
  -e HF_HOME=/app/hf_cache \
  -e QWEN_TTS_FINETUNE_DIR=/app/finetune \
  -e QWEN_TTS_SPEAKER="$SPEAKER" \
  qwen3-tts-voice:1.7b-cu130 \
  python3 -m uvicorn server:app --host 0.0.0.0 --port 8004
