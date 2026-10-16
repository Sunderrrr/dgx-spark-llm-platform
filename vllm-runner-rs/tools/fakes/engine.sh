#!/bin/sh
# A stand-in engine: it announces its startup like vLLM and keeps running.
echo "INFO engine args: $*"
echo "Application startup complete"
exec sleep 600
