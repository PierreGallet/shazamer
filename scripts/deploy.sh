#!/bin/bash
set -e

# Deploy shazamer as a Docker Swarm service (replaces blue-green).
# Run on the genius host (swarm manager). Zero-downtime rolling update:
# update_config.order=start-first keeps the old task serving until the new
# one is healthy, then switches; failure_action=rollback reverts a bad build.
cd "$(dirname "$0")/.."

echo ">> Building shazamer image"
docker build -t shazamer_app:latest .

echo ">> Deploying swarm stack (host/secrets from .env)"
set -a; . ./.env; set +a
docker stack deploy -c docker-stack.yml shazamer

# `docker stack deploy` exits 0 even when the rebuilt
# `shazamer_app:latest` is byte-different from the running one,
# because Swarm needs a registry digest to detect changes and our
# image is local-only. Without this force-recreate the container
# keeps serving old code and CI reports "success" silently. Same
# root cause as the fix in PierreGallet/triton (commit 7d82bea)
# and PierreGallet/AgentMemory (commit a9ca0f7). With
# update_config.order=start-first + failure_action=rollback in
# docker-stack.yml, this stays zero-downtime and auto-reverts.
echo ">> Force task recreate (locally-built image has no registry digest)"
docker service update --force --image shazamer_app:latest shazamer_app

echo ">> Done. Current service:"
docker service ls --filter name=shazamer_app
