#!/bin/bash
# Recrée le conteneur de génération musicale ("music") avec un autre modèle HF.
# Mêmes garanties que ocr-recreate.sh : appelé par vllm-runner via sudo scoped,
# argument unique validé côté Python (forme "org/nom") puis passé en argv, jamais
# interprété par un shell. Aucun port publié — seul dgx-portal l'atteint par music_net.
#
# $1 = model id HuggingFace (ex: MiniMaxAI/MiniMax-Music3)
set -euo pipefail

if [ $# -ne 1 ]; then
  echo "usage: music-recreate.sh <hf_model_id>" >&2
  exit 2
fi

MODEL="$1"
# Garde-fou de forme, en plus de la validation Python : uniquement "org/nom".
case "$MODEL" in
  */*) : ;;
  *) echo "model id invalide : $MODEL" >&2; exit 2 ;;
esac
case "$MODEL" in
  *..*|*" "*|-*) echo "model id invalide : $MODEL" >&2; exit 2 ;;
esac

docker rm -f music >/dev/null 2>&1 || true

# PAS de --memory : mémoire unifiée GB10, une limite plafonnerait aussi la VRAM
#   et ferait échouer le chargement CUDA (cf. CLAUDE.md).
# --cap-drop/--security-opt : ce conteneur exécute du code de modèle tiers
#   téléchargé depuis HF, même durcissement que les autres sidecars.
# Cache HF en écriture : le modèle se télécharge tout seul au premier démarrage,
#   ce qui permet d'ajouter un modèle depuis l'admin sans passer par le shell.
exec docker run -d --name music --restart unless-stopped \
  --network ai-platform_music_net --gpus all --shm-size=2g \
  --pids-limit 512 \
  --security-opt no-new-privileges --cap-drop ALL \
  -v /root/.cache/huggingface-music:/app/hf_cache \
  -e HF_HOME=/app/hf_cache \
  -e MUSIC_MODEL="$MODEL" \
  -e MUSIC_QUANT="${MUSIC_QUANT:-8bit}" \
  ai-platform-music
