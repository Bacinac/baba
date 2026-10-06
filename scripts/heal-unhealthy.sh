#!/usr/bin/env bash
#
# BABA unhealthy-container watchdog.
#
# Docker marks a hard-hung service `unhealthy` when its health beat stops
# (event loop stuck in a libav call, a wedged NATS socket, …) but it does NOT
# auto-restart it — `restart: unless-stopped` only fires on process EXIT, not on
# an unhealthy healthcheck. This closes that gap: it restarts any `baba-*`
# container stuck `unhealthy`. Since the SHM readers now share a /dev/shm VOLUME
# (not the ingestor's IPC namespace / container id), a restart actually HEALS
# instead of failing with a stale-namespace exit 128/255.
#
# SAFETY — this deliberately keys off the container's EXISTING beat-based health:
#   * A container in `start_period` reports `starting`, NOT `unhealthy`, so a
#     detector doing a cold-start TensorRT engine build (its start_period is set
#     long enough to cover a live build) is never touched.
#   * Input-starvation (all cameras offline, upstream outage) does NOT stop the
#     beat — the services stay alive+idle+`healthy` — so a network outage does
#     NOT trigger a restart storm. Only a genuine internal hang does.
#   * Circuit breaker: after MAX_RESTARTS within WINDOW_S, it stops restarting a
#     still-unhealthy container and logs loudly — a hard, visible failure that
#     needs a human, instead of an infinite restart loop.
#
# Invoked every ~60s by baba-healwatch.timer. Logs to journald (tag baba-healwatch).

set -euo pipefail

DEBOUNCE_S=180       # min seconds between restarts of the same container
WINDOW_S=1800        # circuit-breaker observation window (30 min)
MAX_RESTARTS=3       # give up after this many restarts within WINDOW_S
STATE_DIR=/run/baba-healwatch

mkdir -p "$STATE_DIR"
now=$(date +%s)

log() { logger -t baba-healwatch "$*"; }

# --- shared /dev/shm volume mode guard -------------------------------------
# The frame-ring volume must stay world-writable (1777) or the non-root
# ingestor can't create segments and the whole detection pipeline silently
# blinds (recording keeps working — the worst failure mode). docker 29.4.x
# drops the `o: mode=1777` volume option when the tmpfs REMOUNTS after its
# last consumer stops (size= survives, mode= is lost → root:755), so any full
# stack recreate or daemon restart can reintroduce it. The compose `storage-init`
# one-shot normalizes the mode on every `up`; this guard catches the paths
# where storage-init doesn't run (daemon restart auto-restarting the stack).
# Fixed upstream in docker 29.6.x — harmless no-op there.
shm_mp=$(docker volume inspect baba_baba-shm -f '{{.Mountpoint}}' 2>/dev/null || true)
if [ -n "$shm_mp" ] && mountpoint -q "$shm_mp" 2>/dev/null; then
    mode=$(stat -c '%a' "$shm_mp" 2>/dev/null || echo "")
    if [ -n "$mode" ] && [ "$mode" != "1777" ]; then
        chmod 1777 "$shm_mp" && log "fixed baba-shm volume mode ($mode -> 1777)"
    fi
fi
# ---------------------------------------------------------------------------

docker ps --filter 'name=baba-' --format '{{.Names}}' 2>/dev/null | while read -r name; do
    [ -n "$name" ] || continue
    status=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$name" 2>/dev/null || echo none)
    [ "$status" = "unhealthy" ] || continue

    state_file="$STATE_DIR/$name"
    # Load prior restart timestamps, keep only those inside the window.
    kept=""
    if [ -f "$state_file" ]; then
        for ts in $(cat "$state_file" 2>/dev/null); do
            [ $(( now - ts )) -lt "$WINDOW_S" ] && kept="$kept $ts"
        done
    fi
    # shellcheck disable=SC2086
    set -- $kept
    count=$#
    last=0
    [ "$count" -gt 0 ] && last=${!count}

    if [ "$count" -ge "$MAX_RESTARTS" ]; then
        # Circuit breaker: don't loop forever on an unrecoverable container.
        log "CIRCUIT-BREAKER: $name unhealthy after $count restarts in ${WINDOW_S}s — giving up, needs manual intervention"
        continue
    fi
    if [ "$last" -ne 0 ] && [ $(( now - last )) -lt "$DEBOUNCE_S" ]; then
        continue   # restarted very recently; give it time to come up
    fi

    log "restarting unhealthy container $name (restart #$(( count + 1 )) in window)"
    # Record the attempt regardless of outcome so a persistently-FAILING
    # `docker restart` still counts toward the circuit breaker + debounce.
    # Recording only on success meant a hard restart failure never incremented
    # count → breaker never tripped, debounce never engaged → it retried every
    # tick forever with no backoff.
    echo "$kept $now" | tr -s ' ' > "$state_file"
    if docker restart "$name" >/dev/null 2>&1; then
        log "restarted $name"
    else
        log "FAILED to restart $name"
    fi
done
