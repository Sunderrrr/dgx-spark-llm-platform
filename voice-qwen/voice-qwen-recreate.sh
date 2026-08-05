#!/bin/bash
# Recrée le conteneur voix avec Qwen3-TTS. Même principe et mêmes garanties
# que voice-recreate.sh (Chatterbox) : appelé par vllm-runner via sudo scoped,
# argument unique validé contre une liste blanche fermée, jamais interprété
# par un shell.
#
# Les deux moteurs partagent le nom de conteneur « voice » et le réseau
# voice_net : un seul backend voix tourne à la fois, ce qui est voulu sur une
# machine à mémoire unifiée déjà chargée (chat + OCR + vidéo).
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

# Aucun port publié (comme le conteneur OCR et le conteneur Chatterbox) :
# seul dgx-portal l'atteint, par le réseau voice_net.
#
# Le port est forcé à 8004, celui qu'écoute Chatterbox, pour que VOICE_URL
# reste identique quel que soit le moteur : côté portail, seul le champ
# `engine` de /api/model-info distingue les deux.
exec docker run -d --name voice --restart unless-stopped \
  --network ai-platform_voice_net --gpus all --shm-size=4g \
  -v /root/.cache/huggingface:/app/hf_cache \
  -e HF_HOME=/app/hf_cache \
  -e QWEN_TTS_MODEL="Qwen/$MODEL" \
  qwen3-tts-voice:1.7b-cu130 \
  python3 -m uvicorn server:app --host 0.0.0.0 --port 8004
