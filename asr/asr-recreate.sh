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

# --memory : un décodage audio piégé (bombe de décompression) est ainsi tué
#   dans le conteneur au lieu de déclencher l'OOM killer de l'hôte, qui viserait
#   le plus gros consommateur (le modèle de chat).
# --security-opt/--cap-drop : aligne ce conteneur, qui reçoit des octets
#   utilisateur bruts, sur le durcissement du reste de la plateforme.
# PAS de --memory ici : sur le GB10 la mémoire GPU est UNIFIÉE avec la RAM et
#   est comptée dans le cgroup mémoire du conteneur ; une limite --memory
#   plafonne donc aussi les allocations CUDA et fait échouer le chargement du
#   modèle (CUDA out of memory au démarrage). La protection anti-bombe de
#   décompression est assurée dans le code (contrôle d'en-tête avant décodage,
#   server.py) et par le plafond d'octets, pas par le cgroup.
# Le cache HF reste en écriture pour permettre le téléchargement à froid du
#   modèle au premier démarrage (déploiement sans étape manuelle).
exec docker run -d --name asr --restart unless-stopped \
  --network ai-platform_asr_net --gpus all --shm-size=2g \
  --pids-limit 512 \
  --security-opt no-new-privileges --cap-drop ALL \
  -v /root/.cache/huggingface:/app/hf_cache \
  -e HF_HOME=/app/hf_cache \
  -e ASR_MODEL="$MODEL" \
  whisper-asr:turbo-cu130
