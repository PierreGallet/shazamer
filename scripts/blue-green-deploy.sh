#!/bin/bash
set -e

# Blue-Green Deployment Script for Shazamer
# No persistent infrastructure services — just the app container.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
DEPLOY_ENV_FILE="$PROJECT_ROOT/.deploy-env"
INFRA_PATH="${INFRA_PATH:-$HOME/genius}"

echo "======================================"
echo "  Shazamer Blue-Green Deployment"
echo "======================================"

# --- Helper functions ---

wait_for_health() {
    local container_name=$1
    local timeout=${2:-120}
    local counter=0

    echo ">> Waiting for $container_name to be healthy (timeout: ${timeout}s)..."

    while [ $counter -lt $timeout ]; do
        if docker inspect "$container_name" >/dev/null 2>&1; then
            local status
            status=$(docker inspect "$container_name" --format='{{.State.Status}}' 2>/dev/null || echo "not_found")
            local health
            health=$(docker inspect "$container_name" --format='{{.State.Health.Status}}' 2>/dev/null || echo "no_healthcheck")

            if [ "$status" = "running" ] && [ "$health" = "healthy" ]; then
                echo "OK $container_name is healthy"
                return 0
            fi
        fi

        echo "   Waiting... ($((timeout - counter))s left, status=$status, health=$health)"
        sleep 10
        counter=$((counter + 10))
    done

    echo "!! $container_name failed to become healthy within ${timeout}s"
    return 1
}

check_app_health() {
    local env=$1
    local container_name="shazamer_${env}"

    echo ">> Checking health of $env environment"

    # Stage 1: Docker health status
    if ! wait_for_health "$container_name" 120; then
        echo "!! Container health check failed for $env"
        echo ">> Last 50 lines of logs:"
        docker logs --tail 50 "$container_name" 2>&1 || true
        return 1
    fi

    # Stage 2: HTTP health check
    echo ">> Performing HTTP health check for $env environment"
    local attempts=0
    local max_attempts=6

    while [ $attempts -lt $max_attempts ]; do
        if docker exec "$container_name" python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/').read()" >/dev/null 2>&1; then
            echo "OK $env environment HTTP health check passed"
            return 0
        fi
        echo "   HTTP health check attempt $((attempts + 1))/$max_attempts failed, retrying..."
        sleep 10
        attempts=$((attempts + 1))
    done

    echo "!! $env environment HTTP health check failed after $max_attempts attempts"
    echo ">> Last 50 lines of logs:"
    docker logs --tail 50 "$container_name" 2>&1 || true
    return 1
}

cleanup_environment() {
    local env=$1
    echo ">> Cleaning up $env environment"
    COMPOSE_PROJECT_NAME="shazamer_${env}" docker compose -f "$PROJECT_ROOT/docker-compose.${env}.yml" down --remove-orphans 2>/dev/null || true
}

# --- Main deployment flow ---

# 1. Read current state
if [ -f "$DEPLOY_ENV_FILE" ]; then
    ACTIVE_ENV=$(grep "^ACTIVE_ENV=" "$DEPLOY_ENV_FILE" | cut -d= -f2)
fi
echo ">> State file says: ${ACTIVE_ENV:-none}"

# 2. Detect actually running containers
BLUE_RUNNING=$(docker ps --format "{{.Names}}" | grep "shazamer_blue" || echo "")
GREEN_RUNNING=$(docker ps --format "{{.Names}}" | grep "shazamer_green" || echo "")

if [ -n "$BLUE_RUNNING" ]; then
    ACTUAL_CURRENT="blue"
elif [ -n "$GREEN_RUNNING" ]; then
    ACTUAL_CURRENT="green"
else
    ACTUAL_CURRENT=""
fi

echo ">> Actually running: ${ACTUAL_CURRENT:-none}"

# 3. Determine target
if [ "${ACTUAL_CURRENT}" = "blue" ]; then
    TARGET_ENV="green"
    CURRENT_ENV="blue"
elif [ "${ACTUAL_CURRENT}" = "green" ]; then
    TARGET_ENV="blue"
    CURRENT_ENV="green"
else
    TARGET_ENV="blue"
    CURRENT_ENV=""
fi

echo ">> Deploying to: $TARGET_ENV"

# 4. Build image
echo ">> Building application image"
docker build -t shazamer_app:latest "$PROJECT_ROOT"

# 5. Start target environment
echo ">> Starting $TARGET_ENV environment"
COMPOSE_PROJECT_NAME="shazamer_${TARGET_ENV}" docker compose -f "$PROJECT_ROOT/docker-compose.${TARGET_ENV}.yml" up -d

# 6. Health checks
if check_app_health "$TARGET_ENV"; then
    # 7. Switch traffic via infra nginx
    echo ">> Switching traffic to $TARGET_ENV environment"
    bash "$INFRA_PATH/scripts/reload-nginx.sh" shazamer "$TARGET_ENV"

    echo "ACTIVE_ENV=$TARGET_ENV" > "$DEPLOY_ENV_FILE"
    echo "PREVIOUS_ENV=$CURRENT_ENV" >> "$DEPLOY_ENV_FILE"

    if [ -n "$CURRENT_ENV" ]; then
        echo ">> Waiting 10 seconds before cleaning up $CURRENT_ENV environment"
        sleep 10
        cleanup_environment "$CURRENT_ENV"
    fi

    echo "======================================"
    echo "  Deployment SUCCESSFUL"
    echo "  Active environment: $TARGET_ENV"
    echo "======================================"
else
    echo "======================================"
    echo "  Deployment FAILED"
    echo "  Rolling back $TARGET_ENV"
    echo "======================================"

    cleanup_environment "$TARGET_ENV"

    if [ -n "$CURRENT_ENV" ]; then
        echo ">> $CURRENT_ENV environment remains active"
        echo "ACTIVE_ENV=$CURRENT_ENV" > "$DEPLOY_ENV_FILE"
        echo "PREVIOUS_ENV=" >> "$DEPLOY_ENV_FILE"
    fi

    exit 1
fi

# Cleanup old images
echo ">> Cleaning up old Docker images"
docker image prune -f 2>/dev/null || true

echo ">> Blue-Green deployment completed successfully"
