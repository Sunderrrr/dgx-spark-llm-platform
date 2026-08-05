#!/bin/bash
# Recrée le conteneur voix ("voice", Chatterbox TTS) avec une des trois
# variantes du modèle. Appelé par vllm-runner (utilisateur vllmrunner, via
# sudo scoped — voir /etc/sudoers.d/vllmrunner-services) après validation du
# repo_id côté Python (liste blanche fermée, pas d'argument libre) : ce
# script fait confiance à cette validation en amont, à l'identique de
# ocr-recreate.sh pour le conteneur OCR.
#
# Contrairement à OCR (vLLM, choix du modèle en argv), Chatterbox choisit son
# modèle via config.yaml (clé model.repo_id) — ce script régénère donc un
# config.yaml complet à chaque relance plutôt que de passer un argv.
#
# $1 = repo_id (chatterbox | chatterbox-turbo | chatterbox-multilingual)
set -euo pipefail

if [ $# -ne 1 ]; then
  echo "usage: voice-recreate.sh <repo_id>" >&2
  exit 2
fi

REPO_ID="$1"

case "$REPO_ID" in
  chatterbox|chatterbox-turbo|chatterbox-multilingual) ;;
  *) echo "repo_id invalide : $REPO_ID" >&2; exit 2 ;;
esac

STATE_DIR=/var/lib/voice-tts
mkdir -p "$STATE_DIR"/{model_cache,reference_audio,outputs,voices,logs}

cat > "$STATE_DIR/config.yaml" <<EOF
server:
  host: 0.0.0.0
  port: 8004
  use_auth: false
model:
  repo_id: "$REPO_ID"
tts_engine:
  device: auto
  predefined_voices_path: voices
  reference_audio_path: reference_audio
  default_voice_id: default_sample.wav
paths:
  model_cache: model_cache
  output: outputs
generation_defaults:
  temperature: 0.8
  exaggeration: 0.5
  cfg_weight: 0.5
  seed: 0
  speed_factor: 1.0
  language: en
audio_output:
  format: wav
  sample_rate: 24000
  # L'interface autorise 1 minute d'enregistrement micro (et l'arrêt auto tombe
  # à 60,0x s, pas pile 60) : ce plafond doit rester STRICTEMENT au-dessus,
  # sinon Chatterbox refuse un échantillon que l'UI vient d'inviter à faire.
  # La valeur par défaut amont (30 s) provoquait exactement ça.
  max_reference_duration_sec: 90
  save_to_disk: false
ui:
  title: "Cronos Voice"
  show_language_select: true
  max_predefined_voices_in_dropdown: 20
debug:
  save_intermediate_audio: false
EOF

docker rm -f voice >/dev/null 2>&1 || true

exec docker run -d --name voice --restart unless-stopped \
  --network ai-platform_voice_net --gpus all --shm-size=4g \
  -v "$STATE_DIR/config.yaml:/app/config.yaml" \
  -v "$STATE_DIR/model_cache:/app/model_cache" \
  -v "$STATE_DIR/reference_audio:/app/reference_audio" \
  -v "$STATE_DIR/outputs:/app/outputs" \
  -v "$STATE_DIR/voices:/app/voices" \
  -v "$STATE_DIR/logs:/app/logs" \
  -v /root/.cache/huggingface:/app/hf_cache \
  -e HF_HOME=/app/hf_cache \
  chatterbox-voice:v2.0.0-cu130
