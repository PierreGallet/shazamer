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


# Bind mounts fail the whole service if the host path is missing, and the
# stack now mounts a library database and a media store alongside uploads.
echo ">> Ensuring host state directories"
mkdir -p /home/sharon/shazamer/{data,media,uploads,tmp,redis,downloads}
# slskd's own state. What it *shares* is shazamer's downloads directory, so
# the server offers back the tracks it has taken.
mkdir -p /home/sharon/slskd/{config,downloads}

echo ">> Building shazamer image"
docker build -t shazamer_app:latest .

echo ">> Deploying swarm stack (host/secrets from .env)"
set -a; . ./.env; set +a

# Written here, after the .env is loaded — not before it. The first version of
# this sat above the load, read an unset SLSKD_API_KEY, skipped its own
# condition and wrote nothing, while reporting success. slskd then rejected a
# key that was never registered.
# slskd's API key has to be written into its config file. Its environment
# mapping does not reach dictionary entries, so SLSKD_API_KEYS__name__key
# looks plausible, is accepted silently, and registers nothing — every request
# then comes back "rejected the API key" while the key is demonstrably correct.
#
# Rewritten on each deploy rather than appended, so rotating the key works and
# repeated deploys do not stack duplicate blocks.
if [ -n "${SLSKD_API_KEY:-}" ]; then
  SLSKD_CFG=/home/sharon/slskd/config/slskd.yml
  touch "$SLSKD_CFG"
  # Drop any block we wrote before, keeping whatever slskd manages itself.
  awk '/^# >>> shazamer api key/{skip=1} !skip{print} /^# <<< shazamer api key/{skip=0}' \
      "$SLSKD_CFG" > "$SLSKD_CFG.new" 2>/dev/null || cp "$SLSKD_CFG" "$SLSKD_CFG.new"
  {
    echo "# >>> shazamer api key (managed by deploy.sh — edits here are lost)"
    echo "web:"
    echo "  authentication:"
    echo "    api_keys:"
    echo "      shazamer:"
    echo "        key: ${SLSKD_API_KEY}"
    echo "        role: readwrite"
    echo "        cidr: 0.0.0.0/0,::/0"
    echo "# <<< shazamer api key"
  } >> "$SLSKD_CFG.new"
  mv "$SLSKD_CFG.new" "$SLSKD_CFG"
  chmod 600 "$SLSKD_CFG"
  echo ">> slskd API key written to its config"
fi

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
# Every service running the application image needs this, not just the API.
# The worker was left out when it was added, so deploys updated the API and
# silently left the worker on whatever code it started with — including
# through a fix written specifically to unstick it.
echo ">> Force task recreate (locally-built image has no registry digest)"
for svc in shazamer_app shazamer_worker; do
  echo "   $svc"
  docker service update --force --image shazamer_app:latest "$svc"
done

# slskd reads its config once, at startup. Writing the API key above changes
# nothing until it restarts — which `docker stack deploy` will not do, because
# neither its image nor its service definition changed. The key was correct,
# written correctly, and still rejected on every call.
if docker service ls --filter name=shazamer_slskd --format '{{.Name}}' | grep -q .; then
  echo ">> Restarting slskd so it picks up its config"
  docker service update --force shazamer_slskd
fi

echo ">> Done. Current service:"
docker service ls --filter name=shazamer_app
