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

echo ">> Done. Current service:"
docker service ls --filter name=shazamer_app
