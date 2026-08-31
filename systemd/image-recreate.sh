#!/bin/bash
# Recrée le conteneur de génération d'image (diffusers). Mêmes garanties que
# ocr-recreate.sh / voice-recreate.sh / asr-recreate.sh : appelé par
# vllm-runner via sudo scoped, argument unique validé contre une liste blanche
# fermée, jamais interprété par un shell. Aucun port publié — seul dgx-portal
# l'atteint, par image_net (IMAGE_URL=http://image:8007).
#
# $1 = model id (liste blanche ci-dessous). Chaque modèle correspond à un
#      dossier diffusers déjà présent sur l'hôte sous /root/models/<slug> : le
#      téléchargement à froid est une étape de provisioning séparée, jamais
#      déclenchée depuis le web.
set -euo pipefail

if [ $# -ne 1 ]; then
  echo "usage: image-recreate.sh <model_id>" >&2
  exit 2
fi

MODEL="$1"
# slug = dossier local ; NAME = étiquette affichée dans l'UI ; STEPS/GUIDANCE =
# réglages d'inférence propres au modèle. Un modèle DISTILLÉ (few-step) se
# contente de ~8 étapes à guidage 1.0 — le pousser plus haut le sursature. Un
# modèle COMPLET en demande 35 à 50 avec un guidage de 4 à 6. Aucune valeur par
# défaut ne convient aux deux, d'où ce réglage par entrée.
case "$MODEL" in
  black-forest-labs/FLUX.2-klein-4B)
    SLUG="flux2-klein-4b"; NAME="FLUX.2 Klein 4B"
    STEPS=4; GUIDANCE=1.0 ;;
  *) echo "model id invalide : $MODEL" >&2; exit 2 ;;
esac

MODEL_DIR="/root/models/$SLUG"
if [ ! -f "$MODEL_DIR/model_index.json" ]; then
  echo "modèle absent sur l'hôte : $MODEL_DIR (télécharge-le d'abord)" >&2
  exit 3
fi

docker rm -f image >/dev/null 2>&1 || true

# PAS de --memory : sur le GB10 la mémoire GPU est UNIFIÉE avec la RAM et est
#   comptée dans le cgroup mémoire du conteneur ; une limite plafonnerait donc
#   aussi les allocations CUDA et ferait échouer le chargement (~16 Go pour
#   FLUX.2 Klein 4B).
# --cap-drop/--security-opt : ce conteneur reçoit un prompt utilisateur vers du
#   code de modèle tiers, même durcissement que les autres sidecars.
# Modèle monté en lecture seule ; seul dgx-portal atteint le port 8007 (image_net).
exec docker run -d --name image --restart unless-stopped \
  --network ai-platform_image_net --gpus all --shm-size=2g \
  --pids-limit 512 \
  --security-opt no-new-privileges --cap-drop ALL \
  -v "$MODEL_DIR":/model:ro \
  -v /root/models/esrgan:/esrgan:ro \
  -e MODEL_DIR=/model -e MODEL_NAME="$NAME" \
  -e IMAGE_STEPS="$STEPS" -e IMAGE_GUIDANCE="$GUIDANCE" \
  ai-platform-image-gen
