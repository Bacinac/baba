#!/usr/bin/env bash
# BABA disaster-recovery restore. Reverses scripts/backup.sh.
#
# Usage:
#   scripts/restore.sh [ARCHIVE] [--yes]
#     ARCHIVE  path to a baba-backup-*.tar.gz (default: newest in BABA_BACKUP_HOST)
#     --yes    skip the interactive confirmation (for scripted recovery)
#
# DESTRUCTIVE: pg_restore --clean drops+recreates every object in the target DB,
# and media dirs are overwritten. It refuses to restore an archive taken at a
# schema version NEWER than the migrations this checkout knows (that would leave
# the DB ahead of the code). Bring the code up to that version first.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${BABA_ENV_FILE:-$SCRIPT_DIR/.env}"

envval() { [[ -f "$ENV_FILE" ]] || return 0; sed -n "s/^$1=//p" "$ENV_FILE" | tail -1; }

POSTGRES_USER="${POSTGRES_USER:-$(envval POSTGRES_USER)}"; POSTGRES_USER="${POSTGRES_USER:-baba}"
POSTGRES_DB="${POSTGRES_DB:-$(envval POSTGRES_DB)}"; POSTGRES_DB="${POSTGRES_DB:-baba}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-$(envval POSTGRES_PASSWORD)}"
MEDIA_HOST="${BABA_MEDIA_HOST:-$(envval BABA_MEDIA_HOST)}"; MEDIA_HOST="${MEDIA_HOST:-$SCRIPT_DIR/media}"
STATE_HOST="${BABA_STATE_HOST:-$(envval BABA_STATE_HOST)}"; STATE_HOST="${STATE_HOST:-$SCRIPT_DIR/state}"
BACKUP_HOST="${BABA_BACKUP_HOST:-$(envval BABA_BACKUP_HOST)}"; BACKUP_HOST="${BACKUP_HOST:-$SCRIPT_DIR/backups}"
PG_CONTAINER="${BABA_PG_CONTAINER:-baba-postgres}"

log() { printf '%s baba-restore: %s\n' "$(date -u +%H:%M:%S)" "$*"; }
die() { printf '%s baba-restore ERROR: %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; exit 1; }

ARCHIVE=""; ASSUME_YES=0
for a in "$@"; do
    case "$a" in
        --yes|-y) ASSUME_YES=1 ;;
        *) ARCHIVE="$a" ;;
    esac
done
if [[ -z "$ARCHIVE" ]]; then
    ARCHIVE=$(ls -1t "$BACKUP_HOST"/baba-backup-*.tar.gz 2>/dev/null | head -1 || true)
    [[ -n "$ARCHIVE" ]] || die "no archive given and none found in $BACKUP_HOST"
    log "using newest archive: $ARCHIVE"
fi
[[ -f "$ARCHIVE" ]] || die "archive not found: $ARCHIVE"
docker inspect "$PG_CONTAINER" >/dev/null 2>&1 || die "container $PG_CONTAINER not running"
[[ "$(docker inspect -f '{{.State.Running}}' "$PG_CONTAINER")" == true ]] \
    || die "container $PG_CONTAINER is not running — start the project's postgres service first"

compose=(docker compose --project-directory "$SCRIPT_DIR")
"${compose[@]}" config --quiet || die "the project's Compose configuration is not usable"
configured_pg=$("${compose[@]}" ps -aq postgres)
target_pg=$(docker inspect -f '{{.Id}}' "$PG_CONTAINER")
[[ "$configured_pg" == "$target_pg" ]] \
    || die "container $PG_CONTAINER is not this Compose project's postgres service"
[[ "$(docker inspect -f '{{.State.Health.Status}}' "$PG_CONTAINER")" == healthy ]] \
    || die "container $PG_CONTAINER is not healthy"

STAGE="$(mktemp -d "${TMPDIR:-/tmp}/baba-restore.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
log "extracting …"
tar -C "$STAGE" -xzf "$ARCHIVE"
[[ -f "$STAGE/db.dump" ]] || die "archive has no db.dump (corrupt?)"
[[ -f "$STAGE/manifest.json" ]] || die "archive has no manifest.json"

cat "$STAGE/manifest.json"

# Safety: don't restore a DB newer than the code's migrations.
ARCHIVE_VER=$(sed -n 's/.*"schema_version"[: ]*"\([^"]*\)".*/\1/p' "$STAGE/manifest.json" 2>/dev/null | head -1)
CODE_VER=$(ls -1 "$SCRIPT_DIR/db/migrations"/*.sql 2>/dev/null | xargs -n1 basename 2>/dev/null | sed 's/\.sql$//' | sort | tail -1)
[[ -n "$ARCHIVE_VER" && "$ARCHIVE_VER" != "unknown" && -n "$CODE_VER" ]] \
    || die "archive or checkout has no known schema version"
if [[ -n "$ARCHIVE_VER" ]]; then
    # Lexicographic max: if the archive version sorts AFTER the newest local
    # migration, the backup is ahead of this checkout.
    newest=$(printf '%s\n%s\n' "$ARCHIVE_VER" "$CODE_VER" | sort | tail -1)
    if [[ "$newest" == "$ARCHIVE_VER" && "$ARCHIVE_VER" != "$CODE_VER" ]]; then
        die "archive schema '$ARCHIVE_VER' is NEWER than this checkout's newest migration '$CODE_VER'. Update the code first."
    fi
fi

log "verifying the complete database archive …"
docker exec -i -e PGPASSWORD="$POSTGRES_PASSWORD" "$PG_CONTAINER" \
    pg_restore -f /dev/null < "$STAGE/db.dump" >/dev/null \
    || die "database archive is not fully readable — no services or data were changed"

if [[ "$ASSUME_YES" != "1" ]]; then
    echo
    echo "This will DROP + recreate every object in Postgres DB '$POSTGRES_DB' and"
    echo "overwrite media dirs under '$MEDIA_HOST'. This is IRREVERSIBLE."
    read -r -p "Type 'restore' to proceed: " ans
    [[ "$ans" == "restore" ]] || die "aborted"
fi

mapfile -t app_services < <("${compose[@]}" config --services | sed '/^postgres$/d')
if (( ${#app_services[@]} )); then
    log "stopping app services for a clean restore (postgres stays up) …"
    "${compose[@]}" stop "${app_services[@]}" \
        || die "could not stop all app services — database restore was not started"
fi

log "restoring Postgres ($POSTGRES_DB) …"
# Preflight cannot catch target-side SQL errors; keep every DROP and COPY in one transaction.
if ! docker exec -i -e PGPASSWORD="$POSTGRES_PASSWORD" "$PG_CONTAINER" \
    pg_restore --clean --if-exists --no-owner --exit-on-error --single-transaction \
    -U "$POSTGRES_USER" -d "$POSTGRES_DB" < "$STAGE/db.dump"; then
    die "pg_restore FAILED (single transaction). App services remain stopped — verify the transaction outcome before restarting them."
fi
log "Postgres restore OK"

if [[ -d "$STAGE/media" ]]; then
    log "restoring media (reference_photos, scene_crops) …"
    mkdir -p "$MEDIA_HOST"
    for d in "$STAGE"/media/*/; do
        [[ -d "$d" ]] || continue
        name=$(basename "$d")
        cp -a "$d" "$MEDIA_HOST/"
        log "  restored media/$name"
    done
fi

# Secrets (env / state_api) — only present in archives taken after the backup
# hardening. Restore them for fresh-host DR, but NEVER clobber a secret that
# already exists on this host (an operator may have set up .env deliberately).
if [[ -f "$STAGE/env" && ! -f "$ENV_FILE" ]]; then
    ( umask 077; cp -a "$STAGE/env" "$ENV_FILE" )
    log "restored .env (was missing — fresh-host DR)"
elif [[ -f "$STAGE/env" ]]; then
    log "archive contains .env but one already exists here — left untouched (see $ARCHIVE)"
fi
if [[ -d "$STAGE/state_api" && ! -e "$STATE_HOST/api" ]]; then
    mkdir -p "$STATE_HOST"
    cp -a "$STAGE/state_api" "$STATE_HOST/api"
    log "restored state/api (JWT signing key — was missing)"
elif [[ -d "$STAGE/state_api" ]]; then
    log "archive contains state/api but one already exists here — left untouched"
fi

log "bringing the stack back up …"
"${compose[@]}" up -d \
    || die "restore succeeded but 'docker compose up -d' failed — start the stack manually"
log "done — stack restored and restarted."
