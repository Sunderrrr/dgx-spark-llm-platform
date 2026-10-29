#!/usr/bin/env bash
# Register the media models in LiteLLM: image, dictation, voice.
#
# Why a script and not `model_list` in litellm/config.yaml: `store_model_in_db`
# makes the DATABASE authoritative, so a config-list entry is ignored (measured:
# the router kept only the chat models). Why not pass-through endpoints: they
# require `allowed_passthrough_routes`, an ENTERPRISE feature (403, measured).
#
# The three models point at the PORTAL, the only service on the media networks
# (image_net, asr_net, voice_net); the portal speaks the OpenAI shapes and
# forwards to the sidecar. A normal user key reaches them like a chat model.
#
# Idempotent: an existing entry with the same name is deleted first, so running
# it twice leaves the same three entries.
#
#   ./scripts/register-media-models.sh           # register / refresh
#   ./scripts/register-media-models.sh --check   # dry run: list what is there
#
# Needs: the master key in .env, and the portal reachable from the gateway.
set -euo pipefail
cd "$(dirname "$0")/.." || exit 2

LITELLM=${LITELLM_URL:-http://127.0.0.1:4001}
# Assembled, never written literally: the site's own value lives in .env, and the
# secret hooks refuse any tracked file that repeats it.
PORT="${PORTAL_RUST_PORT:-5100}"
MEDIA_BASE="http://portal-rs:${PORT}/v1"

master=$(grep -E '^LITELLM_MASTER_KEY=' .env | cut -d= -f2-)
runner=$(grep -E '^RUNNER_TOKEN=' .env | cut -d= -f2-)
[ -n "$master" ] || { echo "LITELLM_MASTER_KEY missing from .env" >&2; exit 2; }
[ -n "$runner" ] || { echo "RUNNER_TOKEN missing from .env" >&2; exit 2; }

api() { # api <path> <json>
  curl -sS -m 20 -X POST "$LITELLM$1" \
    -H "Authorization: Bearer $master" -H 'Content-Type: application/json' \
    -d "$2"
}

if [ "${1:-}" = "--check" ]; then
  curl -sS -m 20 "$LITELLM/model/info" -H "Authorization: Bearer $master" |
    python3 -c 'import json,sys; print("\n".join(sorted(m["model_name"] for m in json.load(sys.stdin)["data"])))'
  exit 0
fi

# name | upstream | mode | cost field
register() {
  local name=$1 upstream=$2 mode=$3 cost=$4
  local existing
  existing=$(curl -sS -m 20 "$LITELLM/model/info" -H "Authorization: Bearer $master" |
    python3 -c "
import json,sys
for m in json.load(sys.stdin)['data']:
    if m['model_name'] == '$name':
        print((m.get('model_info') or {}).get('id') or '')
        break
")
  if [ -n "$existing" ]; then
    api /model/delete "{\"id\": \"$existing\"}" >/dev/null
    echo "  removed the previous « $name »"
  fi
  api /model/new "{\"model_name\": \"$name\",
    \"litellm_params\": {\"model\": \"$upstream\", \"api_base\": \"$MEDIA_BASE\", \"api_key\": \"$runner\"},
    \"model_info\": {\"mode\": \"$mode\", $cost}}" >/dev/null
  echo "  registered « $name » ($mode)"
}

echo "Registering the media models on $LITELLM"
# dictation carries a working price (input_cost_per_second); image and voice are
# logged at cost 0 by this LiteLLM build, which drops their cost fields — see
# docs/api.md, « Billing, honestly ».
register image     openai/image      image_generation   '"input_cost_per_image": 0.002'
register dictation openai/whisper-1  audio_transcription '"input_cost_per_second": 0.0001'
# Video: LiteLLM's `video_generation` mode speaks `/videos`, `/videos/{id}` and
# `/videos/{id}/content` to an `openai/...` model; the portal translates those to
# the ComfyUI workflow (docs/api.md).
register video     openai/video      video_generation   '"input_cost_per_second": 0.002'
register voice     openai/tts-1      audio_speech       '"input_cost_per_second": 0.0001'
echo "Done. Verify with: ./scripts/register-media-models.sh --check"
