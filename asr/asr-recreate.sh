#!/bin/bash
# Recrée le conteneur de transcription (Whisper). Mêmes garanties que
# ocr-recreate.sh / voice-recreate.sh : appelé par vllm-runner via sudo scoped,
# argument unique validé contre une liste blanche fermée, jamais interprété par
# un shell. Aucun port publié — seul dgx-portal l'atteint, par asr_net.
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

exec docker run -d --name asr --restart unless-stopped \
  --network ai-platform_asr_net --gpus all --shm-size=2g \
  -v /root/.cache/huggingface:/app/hf_cache \
  -e HF_HOME=/app/hf_cache \
  -e ASR_MODEL="$MODEL" \
  whisper-asr:turbo-cu130
