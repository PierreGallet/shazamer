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

#
# Il ATTEND son tour, il ne refuse pas. C'etait un refus immediat (`flock -n`),
# ecrit quand la CI tenait encore la session : faire patienter un runner facture
# a la minute n'avait pas de sens. Maintenant que le deploiement est detache, ce
# refus se retournait contre nous — deux poussees rapprochees lancent deux
# deploiements en parallele, le second etait refuse en SILENCE, et le commit le
# plus recent ne partait jamais. Le serveur restait sur l'avant-dernier, sans
# que rien ne le dise.
#
# En attendant, le second reprend la main quand le premier a fini, refait son
# `git reset --hard` et deploie ce qui est alors le plus recent. Attendre ne
# coute plus rien a personne.
#
# La borne vaut 45 min, la meme fenetre que l'alerte « Deploy started but never
# finished » : au-dela, ce n'est plus une file d'attente, c'est un deploiement
# bloque, et il faut le dire plutot que d'attendre indefiniment.
# One deploy at a time.
#
# Two overlapping runs make Swarm reject the second with "update out of
# sequence": the service's version index moves between the CLI reading the
# spec and sending the update, and that is exactly what a concurrent update
# does. Observed four times in one morning, always in pairs minutes apart —
# because a deploy on this machine takes fifteen to twenty minutes under load,
# and anything that gives up waiting and retries starts a fight rather than a
# queue.
#
# flock, not a pidfile: the lock dies with the process, so a killed deploy
# does not leave the next one blocked for ever.
LOCK_FILE="${DEPLOY_LOCK:-/tmp/deploy-${DEPLOY_SERVICE}.lock}"
# LE VERROU PEUT ETRE DEJA TENU PAR L'APPELANT.
#
# Il ne couvrait que ce script, et c'etait insuffisant : l'enveloppe de
# deploiement fait `git fetch && git reset --hard` AVANT de l'appeler, donc hors
# du verrou. Les deploiements partagent un seul repertoire de travail sur le
# serveur, et l'un pouvait donc reecrire l'arbre pendant qu'un autre le lisait.
#
# Ce n'est pas une hypothese. Le 01/10 : l'image `triton_app:20261001-140307` a
# ete construite depuis le contexte de 7fe4350 alors que l'arbre etait passe a
# 0ab43a5, et sur noctambule `docker buildx` capturait e736c91 pendant que
# l'arbre etait a bd0029e. Une image dont personne ne peut dire quel commit elle
# contient.
#
# L'enveloppe prend donc le verrou AVANT le `reset` et nous passe
# DEPLOY_LOCK_HELD=1. Le reprendre ici nous ferait attendre notre propre verrou.
if [ "${DEPLOY_LOCK_HELD:-0}" = "1" ]; then
    echo ">> Verrou deja tenu par l'appelant, il couvre aussi les operations git." >&2
else
    exec 9>"$LOCK_FILE"
    # Ces messages partent sur stderr et non sur le descripteur 3 que le reste du
    # script utilise : il n'est ouvert que plus bas (`exec 3>&2`), et nyew n'en a
    # pas du tout. Un `echo >&3` sur un descripteur ferme echoue, et `set -e` tue le
    # script — donc la branche « un autre deploiement tourne » mourait sur « Bad
    # file descriptor » au lieu d'attendre. Elle n'avait jamais ete exercee ; elle
    # le sera, maintenant que deux poussees rapprochees se mettent en file.
    if ! flock -n 9; then
      echo ">> Un autre deploiement de $DEPLOY_SERVICE est en cours — on attend son tour."
      echo "   Deux deploiements concurrents se disputeraient les memes services,"
      echo "   et Swarm les rejetterait tous les deux (« update out of sequence »)."
      if ! flock -w "${DEPLOY_LOCK_WAIT:-2700}" 9; then
        echo ">> ECHEC : le verrou de $DEPLOY_SERVICE ne s'est pas libere en ${DEPLOY_LOCK_WAIT:-2700} s."
        echo "   Ce n'est plus une file d'attente : le deploiement precedent est bloque."
        # Pas de `pgrep` ici. Les six depots du serveur nomment tous leur script
        # `deploy.sh`, donc il listait les deploiements des AUTRES piles ; et quand
        # le script est source plutot qu'execute, `$0` vaut `bash` et il rendait
        # tous les processus de la machine — 33 Ko de sortie pour une panne.
        # Le journal du deploiement en cours, lui, nomme la bonne pile.
        echo "   Deploiements en cours : ls -t ~/deploy-logs | head -3" >&2
        exit 75          # EX_TEMPFAIL
      fi
      echo ">> Verrou obtenu, on reprend."
    fi
fi

# ── Le travail est-il encore a faire ? ────────────────────────────────────
#
# Trois fusions rapprochees mettaient trois deploiements en file, chacun
# reconstruisant la MEME image. L'annulation de GitHub n'y change rien : depuis
# que le deploiement est detache, couper le job ne coupe plus le serveur.
#
# La deduplication se decide donc ICI, apres le verrou, quand on sait enfin quel
# commit l'arbre porte. Si c'est deja celui qu'on a deploye avec succes, il n'y a
# rien a faire — « le dernier gagne, les autres s'effacent ».
#
# Le controle vit dans ce script et non dans l'enveloppe pour profiter aussi a
# `make deploy` lance a la main. DEPLOY_FORCE=1 le court-circuite, quand c'est
# l'IMAGE qu'on veut reconstruire et non le code.
DEPLOY_DONE_FILE="${DEPLOY_DONE_FILE:-$HOME/.deploy-done-${DEPLOY_SERVICE}}"
DEPLOY_HEAD="$(git rev-parse HEAD 2>/dev/null || echo inconnu)"
if [ "${DEPLOY_FORCE:-0}" != "1" ] \
   && [ "$DEPLOY_HEAD" != "inconnu" ] \
   && [ "$(cat "$DEPLOY_DONE_FILE" 2>/dev/null)" = "$DEPLOY_HEAD" ]; then
    echo ">> ${DEPLOY_HEAD:0:7} est deja deploye et son deploiement avait reussi." >&2
    echo "   Rien a faire. (DEPLOY_FORCE=1 pour reconstruire quand meme.)" >&2
    exit 0
fi

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
    # Le commit effectivement deploye, memorise UNIQUEMENT en cas de succes :
    # c'est ce que relit le controle d'idempotence au debut. L'ecrire sur un
    # echec ferait sauter le deploiement suivant alors que rien ne tourne.
    if [ "$rc" -eq 0 ] && [ "${DEPLOY_HEAD:-inconnu}" != "inconnu" ]; then
        printf '%s\n' "$DEPLOY_HEAD" > "${DEPLOY_DONE_FILE:-$HOME/.deploy-done-${DEPLOY_SERVICE}}"
    fi
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
    # Written to the log first, then to the caller. fd 3 is the terminal that
    # launched this, and a detached deploy outlives it — so the interesting
    # half of a failure used to vanish with the ssh session, leaving a log
    # that simply stopped mid-sentence with no verdict at the end of it.
    if [ "$rc" -ne 0 ]; then
        echo ">> DEPLOY FAILED (${dur}s), exit $rc"
        echo ">> DEPLOY FAILED (${dur}s) — last 40 lines of $DEPLOY_LOG:" >&3 2>/dev/null || true
        tail -40 "$DEPLOY_LOG" >&3 2>/dev/null || true
    else
        echo ">> DEPLOY OK (${dur}s)"
        echo ">> deploy ok in ${dur}s — $DEPLOY_LOG" >&3 2>/dev/null || true
    fi
    find "$DEPLOY_LOG_DIR" -name "${DEPLOY_SERVICE}-*.log" -mtime +30 -delete 2>/dev/null || true
}
trap _deploy_finish EXIT

# ── Metrique « demarrage » ────────────────────────────────────────────────
#
# Publiee AVANT le travail, la ou `_deploy_finish` publie la fin. Sans elle, un
# deploiement tue net — `kill -9`, OOM killer, redemarrage du serveur pendant un
# build de 18 minutes — ne reecrit jamais la metrique de fin : elle garde son
# ancien `1` et RIEN ne sonne. C'etait GitHub qui attrapait ce cas, avec son
# `timeout-minutes`, en immobilisant un runner tout du long.
#
# Le couple started/finished rend le cas visible sans immobiliser personne : une
# alerte compare les deux et signale un deploiement commence sans verdict.
#
# Ce n'est pas theorique sur cette machine : 54 processus tues par l'OOM killer
# sur le dernier mois, et un build ML qui swappe est une cible de choix.
{
    echo "# HELP genius_deploy_started_timestamp_seconds Unix time the last deploy STARTED."
    echo "# TYPE genius_deploy_started_timestamp_seconds gauge"
    echo "genius_deploy_started_timestamp_seconds{service=\"$DEPLOY_SERVICE\"} $DEPLOY_STARTED_AT"
} > "$DEPLOY_METRICS_DIR/deploy_started_${DEPLOY_SERVICE}.prom.tmp" 2>/dev/null \
  && mv "$DEPLOY_METRICS_DIR/deploy_started_${DEPLOY_SERVICE}.prom.tmp" \
        "$DEPLOY_METRICS_DIR/deploy_started_${DEPLOY_SERVICE}.prom" 2>/dev/null || true



# Bind mounts fail the whole service if the host path is missing, and the
# stack now mounts a library database and a media store alongside uploads.
echo ">> Ensuring host state directories"
mkdir -p /home/sharon/shazamer/{data,media,uploads,tmp,redis,downloads}
# slskd's own state. What it *shares* is shazamer's downloads directory, so
# the server offers back the tracks it has taken.
mkdir -p /home/sharon/slskd/{config,downloads}

echo ">> Building shazamer image"
# zstd rather than the default gzip, and `buildx build` rather than `build`
# because the compression setting only exists on the `--output` form.
#
# This image is built on genius and consumed on genius — it never reaches a
# registry, so gzip buys nothing and costs real minutes. With the containerd
# snapshotter the layers are compressed into the content store and immediately
# decompressed back onto disk, so the host pays for both directions.
#
# Measured here on 2026-09-02, same source, same cache state:
#   gzip   exporting layers 153.0s + unpacking 44.9s = 198s
#   zstd   exporting layers  35.1s + unpacking 31.7s =  67s
# The image also comes out marginally smaller (1.75 GB vs 1.78 GB) and runs
# unchanged — containerd decompresses zstd natively.
#
# No force-compression: cached layers keep the blobs they already have rather
# than being recompressed for nothing. Only newly built layers get zstd.
#
# `name=` in --output is what sets the tag; there is no -t on this form.
docker buildx build --output type=docker,name=shazamer_app:latest,compression=zstd .

# L'EMPREINTE de l'image, relevee tout de suite apres la construction.
#
# Ce depot ne tague qu'en `:latest`, donc comparer des noms d'image ne prouve
# rien : la spec du service et la tache disent « shazamer_app:latest » avant
# comme apres, meme si Swarm a annule la mise a jour et sert encore l'image
# precedente. Seul l'identifiant distingue les deux, et c'est la seule chose
# qu'on puisse verifier ici.
BUILT_IMAGE_ID="$(docker image inspect -f '{{.Id}}' shazamer_app:latest)"
_short() { printf %.12s "${1#sha256:}"; }
echo ">> Image construite : $(_short "$BUILT_IMAGE_ID")"

echo ">> Deploying swarm stack (host/secrets from .env)"
# Read .env by SPLITTING on the first `=`, never by sourcing it.
#
# `.` on a file with an unquoted value containing spaces does not fail — it
# assigns the first word and tries to run the rest as a command. A Gmail app
# password is sixteen characters shown in groups of four, so
# `SMTP_PASSWORD=abcd efgh ijkl mnop` set the password to "abcd" and then
# reported `ijkl: command not found`.
#
# The guard that used to sit here caught exactly that and aborted, which was
# right at the time: the file was hand-edited on the server, and a mangled
# value deserved a stop rather than a silent half-deploy. It stopped being
# right the moment CI began generating the file with `op inject` — the value is
# now correct by construction, and refusing it means refusing a good deploy
# over a parsing choice this script controls.
#
# So parse it properly instead. Values containing spaces, `#` or quotes all
# survive, because nothing is ever handed to the shell to interpret.
while IFS='=' read -r k v; do
    case "$k" in ''|\#*) continue ;; esac
    export "$k=$v"
done < ./.env

# Signing in is the only way in, and a code arrives by mail or not at all.
# Refusing here beats deploying a login nobody can pass and discovering it
# from the outside, locked out of your own library.
missing=""
[ -n "${SMTP_HOST:-}" ]     || missing="$missing SMTP_HOST"
[ -n "${MAIL_FROM:-}" ]     || missing="$missing MAIL_FROM"
[ -n "${SMTP_PASSWORD:-}" ] || missing="$missing SMTP_PASSWORD"
if [ -n "$missing" ]; then
  echo ">> These are empty and sign-in needs them:$missing" >&3
  echo "   Nobody could receive a code, so nobody could get in." >&3
  echo "   Set them in .env and deploy again." >&3
  exit 1
fi

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

# Retried once. "update out of sequence" means the service changed under the
# CLI between reading and writing — a conflict with something else finishing,
# not a bad stack file. Retrying after it settles is the correct response;
# aborting the deploy over it is what left production on old code.
deploy_stack() {
  docker stack deploy -c docker-stack.yml shazamer
}
if ! deploy_stack; then
  echo ">> Stack deploy was rejected; waiting for Swarm to settle and retrying"
  for _ in $(seq 1 24); do
    busy=0
    for svc in shazamer_app shazamer_worker shazamer_slskd; do
      state=$(docker service inspect "$svc" \
                --format '{{.UpdateStatus.State}}' 2>/dev/null || echo "")
      case "$state" in updating|rollback_started) busy=1 ;; esac
    done
    [ "$busy" -eq 0 ] && break
    sleep 5
  done
  deploy_stack
fi

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
# `docker stack deploy` returns before Swarm has finished applying it, so a
# force-update issued immediately races the update already in flight and dies
# with "update out of sequence". That happened, the script reported the worker
# had failed, and the worker was in fact already running the new code — which
# is the worst of both: a scary message that means nothing, next to the exact
# shape of the failure that once left the worker on stale code for a week.
#
# So: wait for the service to settle, then force, then retry once.
for svc in shazamer_app shazamer_worker; do
  echo "   $svc"
  for _ in $(seq 1 30); do
    state=$(docker service inspect "$svc" \
              --format '{{.UpdateStatus.State}}' 2>/dev/null || echo unknown)
    case "$state" in updating|rollback_started) sleep 5 ;; *) break ;; esac
  done
  if ! docker service update --force --image shazamer_app:latest "$svc"; then
    echo "   $svc: force update rejected, settling and retrying once"
    sleep 20
    docker service update --force --image shazamer_app:latest "$svc"
  fi
done

# slskd reads its config once, at startup. Writing the API key above changes
# nothing until it restarts — which `docker stack deploy` will not do, because
# neither its image nor its service definition changed. The key was correct,
# written correctly, and still rejected on every call.
if docker service ls --filter name=shazamer_slskd --format '{{.Name}}' | grep -q .; then
  echo ">> Restarting slskd so it picks up its config"
  docker service update --force shazamer_slskd
fi

# ── Le verdict ────────────────────────────────────────────────────────
# Cette verification vivait dans la CI GitHub, en fin de job « Deploy ». Elle y
# etait au mauvais endroit pour deux raisons.
#
# D'abord elle n'etait pas atteignable par `make deploy` : un deploiement lance
# a la main ne verifiait rien du tout. Ensuite le runner n'attend plus la fin du
# deploiement — il le lance et rend la main — donc la laisser la-bas l'aurait
# fait sonder l'ANCIENNE version, encore en place, et declarer un succes.
#
# Elle est donc ici, la ou le deploiement se termine vraiment, et son code de
# sortie est celui du script : la metrique `genius_deploy_last_success` et
# l'alerte « Last deploy failed » la rapportent.
#
# Placee AVANT la reprise d'espace : `set -e` fait sortir le script ici si la
# verification echoue, et un retour arriere doit retrouver l'image precedente
# et un cache chaud.
echo ">> Verification"
_verified=0
for _ in $(seq 1 30); do
    # Le NOMBRE de repliques d'abord : un conteneur sain ne suffit pas si le
    # service en fait tourner moins qu'il ne devrait. Une sortie propre l'a
    # laisse une fois a 0/1, sans rien pour servir, et un controle qui ne
    # cherchait qu'un conteneur sain aurait appele cela un succes.
    _replicas=$(docker service ls --filter name=shazamer_app --format '{{.Replicas}}')
    _worker=$(docker service ls --filter name=shazamer_worker --format '{{.Replicas}}')
    # Le worker tourne la meme image et fait le travail reel. Un deploiement qui
    # le laisse a terre, ou sur l'ancien code, n'est pas un succes — l'un l'a
    # fait exactement, et le correctif ecrit pour debloquer une analyse n'a
    # jamais atteint le processus qui la tournait.
    _healthy=$(docker ps --filter 'name=shazamer_app' --filter 'health=healthy' -q | head -1)
    # L'image que le conteneur sain fait REELLEMENT tourner. Un service peut
    # etre 1/1 et sain sur l'image PRECEDENTE quand Swarm a annule la nouvelle
    # spec : les repliques ne distinguent pas les deux, l'empreinte oui.
    # `|| true` obligatoire : sous `set -e`, une substitution de commande qui
    # echoue en fin d'affectation tue le script. Sans lui, la premiere iteration
    # ou le conteneur n'est pas encore la interrompait tout le deploiement.
    _live=$( [ -n "$_healthy" ] && docker inspect -f '{{.Image}}' "$_healthy" 2>/dev/null || true )
    _wcid=$( docker ps --filter 'name=shazamer_worker' -q | head -1 || true )
    _live_w=$( [ -n "$_wcid" ] && docker inspect -f '{{.Image}}' "$_wcid" 2>/dev/null || true )
    if [ -n "$_healthy" ] \
       && [ "$_live" = "$BUILT_IMAGE_ID" ] \
       && [ "$_live_w" = "$BUILT_IMAGE_ID" ] \
       && [ "${_replicas%%/*}" = "${_replicas##*/}" ] \
       && [ "${_worker%%/*}" = "${_worker##*/}" ] \
       && docker exec "$_healthy" python -c \
            "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health').read()" \
            >/dev/null 2>&1; then
        echo "   app $_replicas, worker $_worker, /api/health repond, image a jour"
        _verified=1
        break
    fi
    sleep 10
done
if [ "$_verified" != 1 ]; then
    echo ">> ECHEC : aucune tache saine sur la nouvelle image apres 5 minutes" >&2
    echo "   (app: $_replicas worker: $_worker)" >&2
    echo "   image construite : $(_short "$BUILT_IMAGE_ID")" >&2
    echo "   image de l'app   : $(_short "${_live:-aucune}")" >&2
    echo "   image du worker  : $(_short "${_live_w:-aucune}")" >&2
    if [ -n "$_live" ] && [ "$_live" != "$BUILT_IMAGE_ID" ]; then
        echo "   Les repliques sont saines mais sur l'ANCIENNE image : Swarm a" >&2
        echo "   annule la mise a jour. Cause la plus frequente : le conteneur" >&2
        echo "   quitte au demarrage (.env incomplet)." >&2
    fi
    docker service ps shazamer_app --no-trunc 2>/dev/null | head -20 >&2
    _c=$(docker ps --filter 'name=shazamer_app' -q | head -1)
    [ -n "$_c" ] && docker logs "$_c" --tail 50 >&2 2>&1 || true
    exit 1
fi

# ── Reclaim disk ──────────────────────────────────────────────────────
# Placed here, AFTER the rollout: `set -e` means a failed deploy exits before
# this line, so a rollback still finds the previous image and a warm cache.
# What we reap is regenerable by definition — the worst case is a slower next
# build, never a broken one.
#
# This step did not exist at all. shazamer tags only `:latest`, so every
# rebuild orphans the previous image as dangling and leaves the old Swarm
# task's container Shutdown. Those containers cost almost nothing in
# themselves (~1 MB total, measured on genius 2026-08-28) but they PIN their
# image, and that is what stops the nightly cleanup from reclaiming it — it
# logs "in use, skip" and moves on.
echo ">> Reclaiming disk"
docker image prune -f >/dev/null 2>&1 || true
# until=1h, not a bare prune: a container that exited seconds ago may still be
# the rollback target of a rollout that has not finished converging.
docker container prune -f --filter "until=1h" >/dev/null 2>&1 || true

# Conteneurs de tache ARRETES de cette pile, supprimes tout de suite.
#
# Un conteneur arrete epingle son image, et la purge d'images refuse — a
# raison — de toucher une image qu'un conteneur reference. La grace d'une
# heure ci-dessus accordait donc, en pratique, une heure de retention d'image
# par-dessus la politique : le parc gardait courante + precedente au lieu de la
# seule courante.
#
# On assume le deploiement des qu'il est sain, donc on n'attend pas. Ce qu'on
# perd, c'est `docker logs` sur l'ancienne tache ; ses journaux sont dans Loki,
# et `docker service ps` garde l'historique des taches meme sans leur
# conteneur, donc le diagnostic d'un deploiement rate reste lisible.
#
# Cible par NOM, pas un `container prune` global : le meme demon porte les
# cinq autres piles, dont un deploiement peut tourner en meme temps.
docker ps -a --filter "name=shazamer_app."  --filter "name=shazamer_worker." --filter status=exited --format '{{.ID}}' \
    | xargs -r docker rm >/dev/null 2>&1 || true

# Images supplantees de TOUT le parc, pas seulement de ce depot.
#
# Le meme demon Docker porte les images des six depots. Chacun nettoyait les
# siennes et laissait celles des autres au minuteur nocturne de 03:30 — ce qui
# ne tient pas a cinq deploiements dans la journee : triton pese 5 Go l'unite,
# noctambule 4,3 Go, et le disque franchit le seuil d'alerte bien avant que le
# minuteur ne se reveille.
#
# Le script vit dans le depot genius pour qu'il n'existe qu'une politique. S'il
# est absent, on ne fait rien : le nettoyage local ci-dessus a deja fait le plus
# gros, et un deploiement reussi ne doit pas echouer sur du menage.
PRUNE_SUPERSEDED="${PRUNE_SUPERSEDED:-$HOME/genius/scripts/prune-superseded-images.sh}"
if [ -x "$PRUNE_SUPERSEDED" ]; then
    echo ">> Images supplantees (tous depots)"
    "$PRUNE_SUPERSEDED" || true
fi

# NO build-cache prune here. Deliberate, and it reverses what this script did
# earlier the same day.
#
# Measured on genius 2026-09-02, same Dockerfile, same content, back to back:
#
#   rebuild with no prune in between          CACHED = 3 / 3
#   rebuild after `docker builder prune -f`   CACHED = 0 / 3
#
# So a plain prune — no `-a` — destroys the reusable cache outright. The comment
# that used to sit here claimed it only dropped "private" records and spared the
# layers shared with existing images. The LAYERS do survive, being part of those
# images. What does not survive is the cache INDEX: the records mapping a
# Dockerfile step to its layer. Without them BuildKit cannot tell that step N was
# already computed, so it recomputes it. Shared is not the same as reusable, and
# that distinction cost every repo on this host its warm builds.
#
# This ran at the end of every deploy, so each deploy destroyed exactly what the
# next one would have reused — which is why CACHED sat at 0-2 out of ~104 on
# every project here, regardless of Dockerfile or disk space.
#
# Nothing replaces it, because nothing needs to. BuildKit garbage-collects
# itself: `docker buildx inspect default` shows Min Free Space 42.84 GiB, and it
# evicts on its own once free space drops under that. A bounded, self-managing
# resource — the manual prune was redundant AND harmful.
#
# Image retention is unaffected and stays. Deleting the previous image was
# verified NOT to cost the next build its cache (CACHED = 3/3 after `rmi`), so
# the one-image policy and warm builds are compatible.

echo ">> Done. Current service:"
docker service ls --filter name=shazamer_app
