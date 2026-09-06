#!/bin/bash
# Build + deploy the hermes-trader service. The commit hash + date are baked into
# the image automatically by the Dockerfile (no args needed).
set -e
cd "$(dirname "$0")"
docker compose up -d --build
echo "Done. Dashboard: http://umbrel.local:43210/"
