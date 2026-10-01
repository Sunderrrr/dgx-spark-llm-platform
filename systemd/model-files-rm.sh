#!/bin/bash
# Efface du disque les fichiers d'un modèle de chat retiré du catalogue.
# Appelé par vllm-runner (non privilégié) via sudo scoped, quand un admin
# supprime un modèle depuis le portail : les poids appartiennent à root, le
# runner ne peut pas les effacer lui-même.
#
# Mêmes garanties que ocr-recreate.sh / image-recreate.sh : deux arguments,
# revalidés ICI (le runner les a déjà validés, mais ce script est la dernière
# barrière avant un rm -rf en root), jamais interprétés par un shell.
#
#   model-files-rm.sh local <dossier>   → /root/models/<dossier>
#   model-files-rm.sh hf <org>/<nom>    → <hub>/models--<org>--<nom>
#
# Refuse : tout `..`, un lien symbolique, un dossier absent, et les dossiers des
# AUTRES services (gabarits, upscaler, image, voix, transcription) — un modèle
# de chat ne les référence jamais, mais une saisie malheureuse ne doit pas
# pouvoir les emporter.
# Sortie : le nombre d'octets libérés sur stdout.
# Codes : 2 argument invalide, 3 absent, 4 protégé.
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
