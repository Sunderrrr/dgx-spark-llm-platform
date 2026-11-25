#!/bin/bash
# From a recorded dataset to candidate voices you can listen to, on the DGX.
#
#   voice-custom/reference/try_voice.sh <dataset_dir> <run_dir>
#
# Builds four candidate references from the dataset (short, medium, long, and
# a mix of tones), then renders the same test sentences with each one through
# S2 Pro, in a throwaway container of the engine image. Pick the candidate
# that sounds most like you, then:  sudo voice-custom/install-voice.sh <run_dir>/refs/<candidate> <name>
#
# S2PRO_COMPILE=0 measures without torch.compile (eager): on the GB10 the
# compilation alone runs for tens of minutes before any audio comes out.
#
# CANDIDATES overrides the sets: "name=clip1,clip2;name2=clip3,clip4" (clip
# names as in metadata.csv). The defaults use the conversational text 0904
# and, for « varie », short replies in several tones — only clips whose
# transcript matched the script word for word: S2 Pro needs the EXACT text.
set -euo pipefail

if [ $# -ne 2 ]; then
  sed -n 2,13p "$0" >&2
  exit 2
fi
DATASET=$(readlink -f "$1")
RUN=$(readlink -m "$2")
IMAGE=fish-s2pro-voice:0f8a12e-cu130
NEED_GB=${NEED_GB:-24}

[ -f "$DATASET/metadata.csv" ] || { echo "no metadata.csv in $DATASET" >&2; exit 2; }
docker image inspect "$IMAGE" >/dev/null 2>&1 || {
  echo "build the engine image first: docker build -f voice-custom/Dockerfile -t $IMAGE voice-custom/" >&2; exit 1; }
avail_gb=$(awk '/MemAvailable/ {printf "%d", $2/1048576}' /proc/meminfo)
if [ "$avail_gb" -lt "$NEED_GB" ]; then
  echo "only ${avail_gb} GiB available, ~${NEED_GB} GiB needed (S2 Pro ~17 GiB + headroom)" >&2
  exit 1
fi

DEFAULT="court=mael_0904_01.wav,mael_0904_02.wav,mael_0904_03.wav"
DEFAULT+=";moyen=mael_0904_01.wav,mael_0904_02.wav,mael_0904_03.wav,mael_0904_04.wav,mael_0904_05.wav"
DEFAULT+=";long=mael_0904_01.wav,mael_0904_02.wav,mael_0904_03.wav,mael_0904_04.wav,mael_0904_05.wav,mael_0904_06.wav,mael_0904_07.wav,mael_0904_08.wav"
DEFAULT+=";varie=mael_0301.wav,mael_0302.wav,mael_0347.wav,mael_0416.wav,mael_0556.wav,mael_0903_05.wav,mael_0901_06.wav"
CANDIDATES=${CANDIDATES:-$DEFAULT}

mkdir -p "$RUN/refs" "${HOME}/.cache/huggingface"
# Runs as the calling user (with --cap-drop ALL, root in the container could
# not write into the user's directory). NO --memory: unified memory.
run() {
  docker run --rm --gpus all --shm-size=4g \
    --user "$(id -u):$(id -g)" -e HOME=/tmp \
    --security-opt no-new-privileges --cap-drop ALL \
    -v "$DATASET:/data:ro" -v "$RUN:/out" \
    -v "${HOME}/.cache/huggingface:/app/hf_cache" -e HF_HOME=/app/hf_cache -e HF_HUB_OFFLINE=0 \
    "$IMAGE" "$@"
}

IFS=';' read -ra SETS <<< "$CANDIDATES"
for set in "${SETS[@]}"; do
  name=${set%%=*}
  clips=${set#*=}
  [[ "$name" =~ ^[a-z0-9_-]{1,32}$ ]] || { echo "candidate name « $name »: a-z 0-9 _ -" >&2; exit 2; }
  run python3 /app/reference/build_reference.py --dataset /data --clips "$clips" --out "/out/refs/$name"
done
# torch.compile (Inductor) on the GB10 compiles for tens of minutes before the
# first token is emitted (measured 2026-11-25: still compiling after 20 min on
# sm_121, no audio produced). S2PRO_COMPILE=0 measures the eager path instead.
COMPILE_FLAG=""
[ "${S2PRO_COMPILE:-0}" = "0" ] && COMPILE_FLAG="--no-compile"
run python3 /app/reference/test_voice.py --refs /out/refs --out /out/samples $COMPILE_FLAG

echo
echo "Listen to $RUN/samples/*/ and install the candidate you prefer:"
echo "  sudo voice-custom/install-voice.sh $RUN/refs/<candidate> mael"
