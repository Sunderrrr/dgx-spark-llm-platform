#!/bin/bash
# Recrée le conteneur OCR ("ocr") avec un autre modèle HF vLLM. Appelé par
# vllm-runner (utilisateur vllmrunner, via sudo scoped — voir
# /etc/sudoers.d/vllmrunner-services) après validation des flags côté Python
# (_validate_vllm_args, engine="ocr") : ce script fait confiance à cette
# validation en amont et ne fait qu'exécuter la recréation.
#
# $1 = hf_model_id (ex: baidu/Unlimited-OCR)
# $@ (à partir de $2) = flags vLLM déjà validés (liste de tokens, jamais
# interprétés par un shell : docker run les reçoit comme argv du conteneur,
# pas comme options docker — docker arrête de parser des options au nom d'image).
#
# Durcissement (audit M2, 2026-08) — l'OCR est le SEUL sidecar autorisé à passer
# --trust-remote-code, donc à exécuter du code de modèle tiers arbitraire :
#   --cap-drop ALL / --security-opt no-new-privileges : aligne l'OCR sur asr/voice ;
#     ce code arbitraire ne dispose d'aucune capability ni élévation.
#   Cache HF DÉDIÉ (/root/.cache/huggingface-ocr) au lieu de partager en RW le
#     cache du runner hôte (qui sert le modèle de CHAT) : supprime le chemin
#     d'empoisonnement (un repo OCR malveillant ne peut plus écrire dans le cache
#     lu par le runner). Le modèle OCR se (re)télécharge dans ce dossier isolé.
#   PAS de --memory : mémoire unifiée GB10 (plafonnerait la VRAM). Cf. CLAUDE.md.
set -euo pipefail

if [ $# -lt 1 ]; then
  echo "usage: ocr-recreate.sh <hf_model_id> [vllm-flags...]" >&2
  exit 2
fi

HF_ID="$1"
shift

OCR_CACHE=/root/.cache/huggingface-ocr
mkdir -p "$OCR_CACHE"

docker rm -f ocr >/dev/null 2>&1 || true

exec docker run -d --name ocr --restart unless-stopped \
  --network ai-platform_ocr_net --gpus all --shm-size=8g \
  --security-opt no-new-privileges --cap-drop ALL \
  -v "$OCR_CACHE":/root/.cache/huggingface \
  -e HF_HOME=/root/.cache/huggingface \
  vllm/vllm-openai:unlimited-ocr \
  "$HF_ID" "$@"
