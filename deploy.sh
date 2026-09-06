#!/bin/bash
# Build + deploy the hermes-trader service, baking the current commit + date
# into the image so the dashboard footer can show version / commit / freshness.
set -e
cd "$(dirname "$0")"

export GIT_COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo unknown)
export GIT_DATE=$(git log -1 --format=%cI 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ)

echo "Building hermes-trader @ ${GIT_COMMIT} (${GIT_DATE})"
docker compose up -d --build
echo "Done. Dashboard: http://umbrel.local:43210/"
