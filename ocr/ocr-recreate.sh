#!/bin/bash
# Recreate the OCR container ("ocr") with another HF vLLM model. Called by
# vllm-runner (user vllmrunner, via scoped sudo — see
# /etc/sudoers.d/vllmrunner-services) after the flags were validated on the
# Python side (_validate_vllm_args, engine="ocr"): this script trusts that
# upstream validation and only performs the recreation.
#
# $1 = hf_model_id (ex: baidu/Unlimited-OCR)
# $@ (from $2) = already-validated vLLM flags (token list, never interpreted
# by a shell: docker run receives them as the container's argv, not as docker
# options — docker stops parsing options at the image name).
#
# Hardening (audit M2, 2026-08) — OCR is the ONLY sidecar allowed to pass
# --trust-remote-code, i.e. to run arbitrary third-party model code:
#   --cap-drop ALL / --security-opt no-new-privileges: aligns OCR on asr/voice;
#     that arbitrary code gets no capability and no elevation.
#   DEDICATED HF cache (/root/.cache/huggingface-ocr) instead of sharing in RW
#     the host runner's cache (which serves the CHAT model): removes the
#     poisoning path (a malicious OCR repo can no longer write into the cache
#     read by the runner). The OCR model (re)downloads into this isolated
#     folder.
#   NO --memory: GB10 unified memory (would cap the VRAM). See CLAUDE.md.
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

# No `exec`: the L2 filter must be laid down AGAIN AFTER the creation. Docker
# reassigns an IP at every recreation, and a rule pinned to the old one would
# stop blocking anything WITHOUT anything reporting it (silent failure).
# Log cap identical to the compose services (20 MB x 5): without it the
# container log grows unbounded, and a panicked `docker logs` becomes
# unreadable. Applied at next startup, no downtime here.
docker run -d --name ocr --restart unless-stopped \
  --log-opt max-size=20m --log-opt max-file=5 \
  --network ai-platform_ocr_net --gpus all --shm-size=8g \
  --security-opt no-new-privileges --cap-drop ALL \
  -v "$OCR_CACHE":/root/.cache/huggingface \
  -e HF_HOME=/root/.cache/huggingface \
  vllm/vllm-openai:unlimited-ocr@sha256:542961a42d9183813819a23ef3a8b50bfb4f5ef7b0fb4f8e4f56edd8445efb18 \
  "$HF_ID" "$@"
rc=$?

# see cronos-ocr-restrict.service: prevents this container (the only one with
# --trust-remote-code) from opening a connection to the portal, which holds the
# master secrets. Best-effort: an OCR that starts without the filter is better
# than an OCR that does not start, but we say so loudly in the log.
if [ -x /usr/local/sbin/ocr-restrict.sh ]; then
  /usr/local/sbin/ocr-restrict.sh add || echo "ATTENTION : filtre ocr->portail NON pose" >&2
else
  echo "ATTENTION : /usr/local/sbin/ocr-restrict.sh absent, filtre ocr->portail NON pose" >&2
fi
exit $rc
