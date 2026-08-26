#!/bin/bash
set -e

# Deploy shazamer as a Docker Swarm service (replaces blue-green).
# Run on the genius host (swarm manager). Zero-downtime rolling update:
# update_config.order=start-first keeps the old task serving until the new
# one is healthy, then switches; failure_action=rollback reverts a bad build.
cd "$(dirname "$0")/.."
# ── Deploy telemetry ──────────────────────────────────────────────────
# Records how long a deploy takes and whether it worked. Neither existed
# before, and both were wanted on 2026-08-26: the only way to time a deploy
# that day was to diff an image tag — which happens to encode the build
# start — against the image's CreatedAt, and two failed rollouts went
# unnoticed for an hour because the output lived only in whichever terminal
# launched it, and that terminal was gone.
#
# Output goes to a FILE, not the caller's terminal. That is a correctness
# fix rather than tidiness: deploys are driven over ssh, and when that
# session goes away the pipe those docker commands stream into has no
# reader. The write blocks and the script wedges, looking exactly like
# "still building" while prod keeps running stale code.
#
# fd 3 keeps a handle on the real stderr, so the failure tail below still
# reaches whoever launched this instead of vanishing into the log it quotes.
DEPLOY_SERVICE="${DEPLOY_SERVICE:-shazamer_app}"
DEPLOY_LOG_DIR="${DEPLOY_LOG_DIR:-$HOME/deploy-logs}"
DEPLOY_METRICS_DIR="${DEPLOY_METRICS_DIR:-$HOME/node_exporter_textfile}"
mkdir -p "$DEPLOY_LOG_DIR" "$DEPLOY_METRICS_DIR"
DEPLOY_LOG="$DEPLOY_LOG_DIR/${DEPLOY_SERVICE}-$(date +%Y%m%d-%H%M%S).log"
DEPLOY_STARTED_AT="$(date +%s)"
echo ">> Full output: $DEPLOY_LOG"
exec 3>&2
exec >"$DEPLOY_LOG" 2>&1

_deploy_finish() {
    rc=$?
    dur=$(( $(date +%s) - DEPLOY_STARTED_AT ))
    ok=0; [ "$rc" -eq 0 ] && ok=1
    # Scraped by node_exporter's textfile collector on the host. Written then
    # mv'd: the collector re-reads that directory on every scrape and would
    # happily parse a half-written file.
    f="$DEPLOY_METRICS_DIR/deploy_${DEPLOY_SERVICE}.prom"
    {
        echo "# HELP genius_deploy_duration_seconds Wall-clock seconds of the last deploy run."
        echo "# TYPE genius_deploy_duration_seconds gauge"
        echo "genius_deploy_duration_seconds{service=\"$DEPLOY_SERVICE\"} $dur"
        echo "# HELP genius_deploy_last_success Whether the last deploy exited 0."
        echo "# TYPE genius_deploy_last_success gauge"
        echo "genius_deploy_last_success{service=\"$DEPLOY_SERVICE\"} $ok"
        echo "# HELP genius_deploy_last_timestamp_seconds Unix time the last deploy finished."
        echo "# TYPE genius_deploy_last_timestamp_seconds gauge"
        echo "genius_deploy_last_timestamp_seconds{service=\"$DEPLOY_SERVICE\"} $(date +%s)"
    } > "$f.tmp" 2>/dev/null && mv "$f.tmp" "$f" 2>/dev/null || true
    if [ "$rc" -ne 0 ]; then
        echo ">> DEPLOY FAILED (${dur}s) — last 40 lines of $DEPLOY_LOG:" >&3
        tail -40 "$DEPLOY_LOG" >&3 2>/dev/null || true
    else
        echo ">> deploy ok in ${dur}s — $DEPLOY_LOG" >&3
    fi
    find "$DEPLOY_LOG_DIR" -name "${DEPLOY_SERVICE}-*.log" -mtime +30 -delete 2>/dev/null || true
}
trap _deploy_finish EXIT


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
