#!/bin/bash
# Erase from disk the files of a chat model removed from the catalog.
# Called by vllm-runner (unprivileged) via scoped sudo, when an admin deletes a
# model from the portal: the weights belong to root, the runner cannot delete
# them itself.
#
# Same guarantees as ocr-recreate.sh / image-recreate.sh: two arguments,
# revalidated HERE (the runner already validated them, but this script is the
# last barrier before a root rm -rf), never interpreted by a shell.
#
#   model-files-rm.sh local <folder>   → /root/models/<folder>
#   model-files-rm.sh hf <org>/<name>  → <hub>/models--<org>--<name>
#
# Refuses: any `..`, a symlink, a missing folder, and the folders of the OTHER
# services (templates, upscaler, image, voice, transcription) — a chat model
# never references them, but an unlucky input must not be able to take them
# out.
# Output: the number of bytes freed on stdout.
# Codes: 2 invalid argument, 3 missing, 4 protected.
set -euo pipefail

if [ $# -ne 2 ]; then
  echo "usage: model-files-rm.sh local|hf <id>" >&2
  exit 2
fi
KIND="$1"
ID="$2"
case "$ID" in
  *..*) echo "id invalide : $ID" >&2; exit 2 ;;
esac

TARGETS=()
case "$KIND" in
  local)
    if ! [[ "$ID" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,60}$ ]]; then
      echo "id invalide : $ID" >&2; exit 2
    fi
    case "$ID" in
      templates|esrgan|flux2-klein-4b) echo "dossier protégé : $ID" >&2; exit 4 ;;
    esac
    T="/root/models/$ID"
    if [ -L "$T" ] || [ ! -d "$T" ]; then
      echo "absent : $T" >&2; exit 3
    fi
    TARGETS+=("$T")
    ;;
  hf)
    if ! [[ "$ID" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,60}/[A-Za-z0-9][A-Za-z0-9._-]{0,80}$ ]]; then
      echo "id invalide : $ID" >&2; exit 2
    fi
    case "$ID" in
      openai/whisper-*|ResembleAI/*|Qwen/Qwen3-TTS-*|black-forest-labs/*)
        echo "dépôt protégé (autre service) : $ID" >&2; exit 4 ;;
    esac
    D="models--${ID/\//--}"
    for H in /root/.cache/huggingface/hub /var/lib/vllm-runner/.cache/huggingface/hub; do
      if [ -d "$H/$D" ] && [ ! -L "$H/$D" ]; then
        TARGETS+=("$H/$D")
      fi
    done
    if [ ${#TARGETS[@]} -eq 0 ]; then
      echo "absent du cache HF : $ID" >&2; exit 3
    fi
    ;;
  *)
    echo "type invalide : $KIND" >&2; exit 2 ;;
esac

OCTETS=$(du -sb -- "${TARGETS[@]}" | awk '{s += $1} END {print s + 0}')
rm -rf --one-file-system -- "${TARGETS[@]}"
echo "$OCTETS"
