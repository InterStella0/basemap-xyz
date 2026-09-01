#!/usr/bin/env bash
# Deploy the swarm stack from the repo root, on the manager (VPS).
#
# `docker stack deploy` reads .env for ${VAR} interpolation from the project directory, but that
# lookup depends on the CWD you invoke it from. Sourcing .env first (a) pins the deploy to the
# repo root's .env regardless of where the shell started, and (b) makes every value visible to
# this shell for anything else the deploy pipeline needs.
#
# Build/push the images first with scripts/push-swarm-images.sh; the stack never builds.
set -euo pipefail

repo_root=$(cd "$(dirname "$0")/.." && pwd)
cd "$repo_root"

if [ ! -f .env ]; then
    echo "deploy-swarm: .env not found in $repo_root; run 'cp default.env .env' and fill in the secrets" >&2
    exit 1
fi

if [ "$(docker info --format '{{.Swarm.ControlAvailable}}' 2>/dev/null)" != "true" ]; then
    echo "deploy-swarm: this node is not a swarm manager; run this on the VPS" >&2
    exit 1
fi

set -a      # export every variable defined while sourcing
# shellcheck disable=SC1091
source ./.env
set +a

echo "==> deploying stack 'dark-basemap' (BASEMAP_PUBLIC_URL=${BASEMAP_PUBLIC_URL:-<unset>})"
docker stack deploy -c compose.swarm.yaml dark-basemap
