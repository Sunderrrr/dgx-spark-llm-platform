#!/bin/bash
# Recreate the voice container ("voice", Chatterbox TTS) with one of the three
# model variants. Called by vllm-runner (user vllmrunner, via scoped sudo — see
# /etc/sudoers.d/vllmrunner-services) after the repo_id was validated on the
# Python side (closed allowlist, no free argument): this script trusts that
# upstream validation, exactly like ocr-recreate.sh for the OCR container.
#
# Unlike OCR (vLLM, model choice in argv), Chatterbox picks its model through
# config.yaml (key model.repo_id) — so this script regenerates a full
# config.yaml at every relaunch rather than passing an argv.
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
  # The UI allows 1 minute of microphone recording (and its auto-stop lands at
  # 60.0x s, not exactly 60): this cap must stay STRICTLY above that,
  # otherwise Chatterbox rejects a sample the UI just invited the user to make.
  # The upstream default (30 s) caused exactly that.
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

# Log cap identical to the compose services (20 MB x 5): without it the
# container log grows unbounded, and a panicked `docker logs` becomes
# unreadable. Applied at next startup, no downtime here.
exec docker run -d --name voice --restart unless-stopped \
  --log-opt max-size=20m --log-opt max-file=5 \
  --security-opt no-new-privileges --cap-drop ALL --pids-limit 512 \
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
