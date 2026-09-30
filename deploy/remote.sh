#!/usr/bin/env bash
# The half of a deploy that runs ON the instance. deploy/deploy.sh ships this
# file and starts it detached; nothing here reaches back to the deploying host.
#
# Shipped as a FILE and run under setsid rather than streamed into `ssh bash -s`,
# because an instance may be reached over ssh through a Cloudflare tunnel that a
# deploy can itself restart: the connection dies mid-recreate, SIGHUP kills docker
# compose, and the stack is left half-up with no way back in. A DIDA instance was
# lost exactly that way on 2026-08-07, over the same kind of ssh route.
#
# What it will not do: build without a verified backup to fall back on, restore
# previous images on top of a schema a destructive migration already changed, or
# report a revision the containers are not running.
#
# Inputs, all from the environment: VARIANT, TARGET_PATH, FLAGS, PULL,
# DESTRUCTIVE (the migrations that make this deploy one-way) and NO_ROLLBACK (a
# sentence saying why the images must not be swapped back, empty when they may).
# Both are decided by the deploying side, which is the one holding a git history.
set -euo pipefail

cd "$TARGET_PATH"
FLAGS=${FLAGS:-}
PULL=${PULL:-0}
DESTRUCTIVE=${DESTRUCTIVE:-}
NO_ROLLBACK=${NO_ROLLBACK:-}

env_read() { [ -f .env ] || return 0; sed -n "s/^$1=//p" .env | tail -1; }

PG_CONTAINER=baba-postgres
PG_USER=$(env_read POSTGRES_USER);     PG_USER=${PG_USER:-baba}
PG_DB=$(env_read POSTGRES_DB);         PG_DB=${PG_DB:-baba}
PG_PASSWORD=$(env_read POSTGRES_PASSWORD)
BACKUP_HOST=$(env_read BABA_BACKUP_HOST); BACKUP_HOST=${BACKUP_HOST:-$PWD/backups}
IMAGE_BASE=$(env_read BABA_IMAGE_BASE);   IMAGE_BASE=${IMAGE_BASE:-ghcr.io/bacinac/baba}
KEEP=${BABA_DEPLOY_BACKUP_KEEP:-5}

# Nothing to dump and nothing to fall back to unless a stack is actually up, and
# both steps below would fail on that rather than on anything being wrong. Two
# instances legitimately arrive here with the stack down: a first deploy, and a
# `park`ed one that was stopped at the end of its previous deploy.
RUNNING=0
if docker compose ps --status running --format '{{.Service}}' 2>/dev/null | grep -qx postgres; then
    RUNNING=1
fi

# --- .env keys the new code needs ----------------------------------------
# Code that needs a new .env key must bring it: an instance whose .env predates
# the key simply refuses to come up ("required variable POSTGRES_PASSWORD is
# missing a value"), and that is a config change nobody would think to make by
# hand on three boxes. envgen_merge_missing only APPENDS what is absent — it
# never touches a value the operator set. The same goes the other way: a key
# the code stopped reading still looks like live configuration, so
# envgen_retire removes it first (carrying over what it can, see there).
if [ -f scripts/lib/envgen.sh ] && [ -f .env ]; then
  (
    set +u
    SCRIPT_DIR="$PWD"
    . scripts/lib/colors.sh
    . scripts/lib/envgen.sh
    envgen_retire .env
    envgen_merge_missing .env
  ) || { echo "ERROR: could not bring .env up to date with this code" >&2; exit 1; }
fi

# --- 1. a backup, before anything can change -----------------------------
# The one step that turns a bad deploy from a disaster into an inconvenience.
# It runs first, so it captures the schema the CURRENT code was written against,
# and a failure here stops the deploy instead of being logged and stepped over.
#
# track_embedding_samples is excluded the same way scripts/backup.sh excludes it:
# tens of GB of raw per-sample re-ID history, regenerable, and dumping it on every
# deploy would put the backup on the critical path for no recoverable value.
if [ "$RUNNING" = 1 ]; then
    BACKUP_DIR="$BACKUP_HOST/pre-deploy"
    mkdir -p "$BACKUP_DIR"
    DUMP="$BACKUP_DIR/baba-$(date -u +%Y%m%dT%H%M%SZ).dump"
    echo "== backing up the database to $DUMP =="
    if ! docker exec -e PGPASSWORD="$PG_PASSWORD" "$PG_CONTAINER" \
           pg_dump -U "$PG_USER" -Fc --no-owner --no-acl \
           --exclude-table-data=track_embedding_samples "$PG_DB" > "$DUMP"; then
        rm -f "$DUMP"
        echo "FAILED: pg_dump did not complete — refusing to deploy without a backup" >&2
        exit 1
    fi
    # A file of the right shape is not proof it is restorable, so the archive is
    # actually READ — every data block decompressed, the SQL thrown away. The
    # cheaper `pg_restore --list` is not enough: a custom-format archive keeps its
    # table of contents near the FRONT, so a dump truncated halfway through the
    # data lists perfectly. A full disk or a killed container produces exactly
    # that dump, and it would have been called a good backup.
    #
    # No filename, so pg_restore reads stdin: naming /dev/stdin looks equivalent
    # and is not — pg_restore reopens the path, cannot seek it, and fails with
    # "did not find magic string in file header" on a perfectly good dump.
    if ! docker exec -i -e PGPASSWORD="$PG_PASSWORD" "$PG_CONTAINER" \
           pg_restore -f /dev/null < "$DUMP" >/dev/null 2>&1; then
        echo "FAILED: the dump is not a readable pg_dump archive — refusing to deploy" >&2
        echo "        (kept at $DUMP for inspection)" >&2
        exit 1
    fi
    # `ls -lh`, not `du -h`: du reports ALLOCATED BLOCKS, and on a network share a
    # freshly written dump reports as "512" — a healthy run whose log reads like a
    # 512-byte backup.
    echo "backup OK — $(ls -lh "$DUMP" | awk '{print $5}'), verified readable"
    ls -1t "$BACKUP_DIR"/baba-*.dump 2>/dev/null | tail -n "+$((KEEP + 1))" | while read -r old; do
        rm -f "$old" && echo "pruned old pre-deploy backup $(basename "$old")"
    done
else
    echo "== no running stack — skipping backup and rollback snapshot =="
    echo "   (a first deploy, or an instance parked at the end of the last one)"
fi

# --- 2. is this deploy reversible by swapping images back? ---------------
# Restoring the previous images puts OLD code on the NEW schema. That is only
# safe while the migrations in between are additive — old code ignores a new
# column. Across a migration that drops a column or a table it turns one broken
# deploy into two, so the deploying side reads the actual diff and says so here.
if [ -n "$NO_ROLLBACK" ]; then
    echo "== NOTE: image rollback is DISABLED for this run =="
    echo "   $NO_ROLLBACK"
    for m in $DESTRUCTIVE; do echo "     $m"; done
fi

# --- 3. snapshot the running images --------------------------------------
# By TAG, not by image id: with the containerd snapshotter a container's .Image
# is a config digest that `docker tag` cannot resolve, so an id-based snapshot
# could never be restored. Pointing :rollback at what is running RIGHT NOW also
# keeps that image alive, so the prune at the end of the previous deploy cannot
# already have taken it.
ROLLBACK_TAGGED=0
if [ "$RUNNING" = 1 ]; then
    echo "== snapshotting the running images =="
    for ref in $(docker compose config --images | sort -u); do
        case "$ref" in "$IMAGE_BASE"/*) ;; *) continue ;; esac
        if docker image inspect "$ref" >/dev/null 2>&1; then
            docker tag "$ref" "${ref%:*}:rollback" && ROLLBACK_TAGGED=$((ROLLBACK_TAGGED+1))
        fi
    done
    if [ "$ROLLBACK_TAGGED" -eq 0 ]; then
        echo "FAILED: could not snapshot any image — refusing to deploy without a way back" >&2
        exit 1
    fi
    echo "rollback snapshot: $ROLLBACK_TAGGED image(s) tagged :rollback"
fi

# --- 4. build ------------------------------------------------------------
# Retried, because a build over a thin uplink fails for a reason that goes away.
# cabin reaches this repo over 0.79 Mbit/s: one dropped download aborts the whole
# `compose build`. Docker's layer cache makes a retry cheap AND progressive —
# every finished layer is reused, so each attempt starts further along.
# NOT a blanket retry: code that does not compile fails identically three times
# and takes three times as long to say so. That is the price of covering the
# failure that actually happens here, and it is bounded.
build_retry() {
    attempt=1
    while true; do
        if "$@"; then return 0; fi
        if [ "$attempt" -ge 3 ]; then
            echo "FAILED: $* did not complete after $attempt attempts" >&2
            return 1
        fi
        echo "build attempt $attempt failed — retrying (cached layers are kept)" >&2
        attempt=$((attempt + 1))
        sleep 5
    done
}

if [ "$PULL" = "1" ]; then
    # Published images (see deploy/publish.sh). The point of this path is hosts
    # too slow to build — it must fail loudly rather than fall back to a build
    # that would take an hour on them.
    build_retry docker compose pull
else
    # The base image sits behind the "bases" profile, so a plain `compose build`
    # skips it and the services silently ride whatever stale base is on the host.
    build_retry docker compose --profile bases build "base-$VARIANT"
    build_retry docker compose build
fi

# --- 5. recreate ---------------------------------------------------------
docker compose down --remove-orphans
case ",$FLAGS," in
  *,recreate_shm,*) docker volume rm -f baba_baba-shm >/dev/null 2>&1 || true ;;
esac
# The service containers run as uid 1000; a root-owned tier means no JWT key,
# no compiled-model cache, no recordings.
chown -R 1000:1000 state logs models/cache 2>/dev/null || true

# A first deploy has nothing to be stale about, and compose bind-mounts this
# path: were it still missing at `up`, Docker would create a DIRECTORY there and
# the api would serve the dev stub forever.
[ -f revision.json ] || cp revision.json.pending revision.json

docker compose up -d --remove-orphans

cat > /etc/systemd/system/baba-nats-fw.service <<EOF
[Unit]
Description=BABA: only its own containers and BABA_NATS_PEER_IPS reach the published NATS ports
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$TARGET_PATH/deploy/nats-fw.sh

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable -q baba-nats-fw.service
deploy/nats-fw.sh

# --- 6. health gate ------------------------------------------------------
# `ps` WITHOUT -a lists only running containers, so a service that died on boot
# simply vanishes from the check and the deploy reports OK. It only ever caught
# anything because every service is `restart: unless-stopped` and a crash-looper
# shows as 'Restarting' — an unstated invariant the guard depended on.
#
# `Exited (0)` is deliberately allowed: shm-init is a one-shot that chmods the
# frame ring and is SUPPOSED to exit clean, so a bare 'Exited' match would fail
# every deploy. A non-zero code is the actual failure, hence the digit class.
health_bad() {
    docker compose ps -a --format '{{.Name}} {{.Status}}' \
      | grep -E 'unhealthy|health: starting|Restarting|Created|Dead|Exited \([1-9]' || true
}

echo "waiting for health..."
bad=""
for _ in $(seq 1 30); do
    sleep 10
    bad=$(health_bad)
    [ -z "$bad" ] && break
done

if [ -n "$bad" ]; then
    echo "NOT HEALTHY:"
    echo "$bad"

    if [ -n "$NO_ROLLBACK" ]; then
        # Refusing IS the correct action. Putting the previous images back here
        # would run code that predates the migration on a schema it cannot read —
        # a second, harder outage on top of the first.
        echo "== NOT rolling back: $NO_ROLLBACK ==" >&2
        for m in $DESTRUCTIVE; do echo "     $m" >&2; done
        echo "   Recover by hand: restore the pre-deploy dump under $BACKUP_HOST/pre-deploy," >&2
        echo "   then deploy the previous commit." >&2
        exit 1
    fi
    if [ "$ROLLBACK_TAGGED" -eq 0 ]; then
        echo "== no rollback snapshot to restore (first deploy) ==" >&2
        exit 1
    fi

    echo "== rolling back to the previous images =="
    restored=0
    for ref in $(docker compose config --images | sort -u); do
        case "$ref" in "$IMAGE_BASE"/*) ;; *) continue ;; esac
        rb="${ref%:*}:rollback"
        if docker image inspect "$rb" >/dev/null 2>&1; then
            docker tag "$rb" "$ref" && restored=$((restored+1))
        fi
    done
    echo "restored $restored image(s) from :rollback"
    docker compose up -d --remove-orphans
    echo "verifying the restored stack..."
    rb_bad=""
    for _ in $(seq 1 12); do
        sleep 10
        rb_bad=$(health_bad)
        [ -z "$rb_bad" ] && break
    done
    # revision.json is never touched on this path: it is still the PREVIOUS
    # deploy's stamp, which is now the truth again, and the new one is sitting
    # unclaimed in revision.json.pending.
    if [ -z "$rb_bad" ]; then
        echo "ROLLBACK OK — previous stack restored"
    else
        echo "ROLLBACK STILL UNHEALTHY:"
        echo "$rb_bad"
    fi
    echo "the pre-deploy backup is under $BACKUP_HOST/pre-deploy"
    exit 1
fi

# A run that snapshotted nothing (a parked instance, a stack that was down)
# leaves any :rollback from an older deploy in place, and no later deploy would
# ever move it: on the parked nvidia box two stale generations of ~16 GB images
# filled the disk and failed the build. Such a tag is older than what now runs.
if [ "$ROLLBACK_TAGGED" -eq 0 ]; then
    for ref in $(docker images --format '{{.Repository}}:{{.Tag}}'); do
        case "$ref" in "$IMAGE_BASE"/*:rollback) docker rmi "$ref" >/dev/null ;; esac
    done
fi
docker image prune -f >/dev/null
docker builder prune -f --reserved-space 10GB >/dev/null
# Everything is healthy, so the shipped revision is now the running one and may
# claim the name. Before this line the box still reports the previous deploy —
# which is the truth while the new one is building or after it has failed.
# Written INTO the file, never moved onto it: the api bind-mounts revision.json
# and a bind mount follows the inode, so `mv` leaves every running container
# reading the old bytes for as long as it lives.
if [ -f revision.json.pending ]; then
    cat revision.json.pending > revision.json && rm -f revision.json.pending
fi
# revision.json is what THIS deploy shipped; the host's own .git is frozen at
# whatever it last pulled (nothing pulls any more) and would report a commit the
# box is not running — prod printed a stale sha exactly once before this flip.
rev=$(sed -n 's/.*"sha": *"\([^"]*\)".*/\1/p' revision.json 2>/dev/null)
[ -n "$rev" ] || rev=unknown
count=$(docker compose ps -aq | wc -l)

# A parked instance has just proven the variant builds, migrates and comes up
# healthy — which is the whole reason it exists. Leaving it running would hold
# GPU memory for a pipeline with nothing to process.
case ",$FLAGS," in
  *,park,*)
    docker compose stop >/dev/null
    echo "OK $rev — $count containers healthy, stack parked"
    exit 0
    ;;
esac
echo "OK $rev — $count containers healthy"
