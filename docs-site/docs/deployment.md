---
id: deployment
title: Deployment
---

# Deployment

Docker Swarm behind Traefik. `scripts/deploy.sh` builds the image, writes what
needs writing, deploys the stack, and forces the services running the
application image to recreate.

## The force-recreate

`docker stack deploy` exits zero even when a rebuilt local image differs from
the running one: Swarm needs a registry digest to detect a change, and a
locally-built image has none. Without forcing, the container keeps serving old
code and CI reports success.

Both `app` and `worker` are forced. The worker was left out when it was added,
so deploys updated the API and silently left the worker on whatever code it
started with — including through a fix written specifically to unstick it.

## Secrets

CI rewrites only the keys it owns and leaves everything else in the server's
`.env` alone. It used to replace the whole file with two lines, so anything set
by hand survived until the next push and then vanished, with nothing to
indicate it had happened.

Both routes work: GitHub Secrets are written when present, and anything placed
on the server directly is preserved. An unset secret leaves what is already
there rather than blanking it.

## The health probe

A raw socket under `python -S` (`scripts/healthcheck.py`), not urllib.
Importing urllib and the site packages cost 1.8 s at idle against 0.64 s, paid
out of the same CPU quota the analysis is saturating. It timed out under normal
load and Swarm killed healthy containers mid-analysis.

Tolerances match what the service does: analysis is CPU-bound by design, so a
probe that is merely slow is not evidence of a sick process. Twenty seconds,
five retries — about two minutes of real silence before a container is
replaced.

The worker has **no** health probe. It inherited the image's, which asks about
an HTTP port only the API serves, so it failed forever and Swarm killed it in a
loop. Nothing replaced it: arq reconnects to Redis by itself, a dead process is
caught by the restart policy, and a probe that can be wrong has cost enough.

## Restart policy

`condition: any`, not `on-failure`. A web service has no successful exit — if
the process is gone, the site is down whatever the exit code says. Under
`on-failure` a clean shutdown left the task marked complete and Swarm declined
to replace it, so the service sat at 0/1 and every request 404'd until someone
forced it back.

## Resources

| Service | CPU | Memory |
| --- | --- | --- |
| app | 1.0 | 1 GB |
| worker | 4.0 | 2 GB |
| redis | 0.5 | 256 MB |
| slskd | 0.5 | 512 MB |

Limits, not reservations, on a box shared with several other projects. Under
load the analysis simply takes longer.

slskd is capped deliberately low. A client that cannot log in retries in a
tight loop, and that loop once took a whole core from the analysis worker while
accomplishing nothing — the load average reached 20 on eight cores. Capped, the
same failure is slow rather than expensive.

The worker's 2 GB is headroom over a **measurement** — about 550 MB mid-analysis
on a sixty-nine minute set. An earlier limit was set from a `tracemalloc`
figure, which measures Python allocations rather than resident memory, and was
an order of magnitude too low. Size a container from RSS under real load.

## slskd's API key

Written into `slskd.yml` by the deploy, not passed as an environment variable.
slskd's environment mapping does not reach dictionary entries, so
`SLSKD_API_KEYS__name__key` looks plausible, is accepted silently, and
registers nothing — every call then returns "rejected the API key" while the
key is demonstrably correct.

It is written *after* the `.env` is loaded, and slskd is restarted afterwards:
it reads its configuration once, at startup, and `docker stack deploy` will not
restart it when neither its image nor its definition changed.
