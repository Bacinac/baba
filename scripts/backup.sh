#!/usr/bin/env bash
# BABA disaster-recovery backup.
#
# Snapshots ONLY the irreplaceable data:
#   - Postgres DB (identities, embeddings, config, users, events, audit) via
#     pg_dump custom format.
#   - media/reference_photos + media/scene_crops — user-uploaded / few-shot
#     prototype images that CANNOT be regenerated.
#
# Deliberately EXCLUDED (ephemeral — regenerable from tracks or age out):
#   segments/ (video), crops/, face_crops/, thumbnails/, clips/. Backing up
#   TB of video every night is neither useful nor practical.
#
# This is a cold, point-in-time DR archive — NOT a parallel live store. It
# exists because a media-tier migration once silently lost every identity
# reference photo (embeddings survived in Postgres, the JPEGs did not).
#
# Output: $BABA_BACKUP_HOST/baba-backup-<UTC>.tar.gz  (+ retention pruning).
# Schedule it with the systemd timer install.sh offers, or cron. Restore with
# scripts/restore.sh.
set -euo pipefail

# BABA_CHECKOUT_DIR lets the root-owned systemd copy (installed outside the
# checkout so the operator can't edit code that runs as root) still resolve
# .env and the on-disk tiers from the real checkout. Falls back to this
# script's own parent for a manual run from inside the checkout.
SCRIPT_DIR="${BABA_CHECKOUT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ENV_FILE="${BABA_ENV_FILE:-$SCRIPT_DIR/.env}"

envval() {  # read KEY from .env without sourcing it (values may contain spaces)
    [[ -f "$ENV_FILE" ]] || return 0
    sed -n "s/^$1=//p" "$ENV_FILE" | tail -1
}

POSTGRES_USER="${POSTGRES_USER:-$(envval POSTGRES_USER)}"; POSTGRES_USER="${POSTGRES_USER:-baba}"
POSTGRES_DB="${POSTGRES_DB:-$(envval POSTGRES_DB)}"; POSTGRES_DB="${POSTGRES_DB:-baba}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-$(envval POSTGRES_PASSWORD)}"
MEDIA_HOST="${BABA_MEDIA_HOST:-$(envval BABA_MEDIA_HOST)}"; MEDIA_HOST="${MEDIA_HOST:-$SCRIPT_DIR/media}"
STATE_HOST="${BABA_STATE_HOST:-$(envval BABA_STATE_HOST)}"; STATE_HOST="${STATE_HOST:-$SCRIPT_DIR/state}"
PG_CONTAINER="${BABA_PG_CONTAINER:-baba-postgres}"

# Where archives land. Default is a sibling dir, but for real DR this SHOULD
# point at a different physical disk/host than the data (a disk loss that takes
# both the data and its only backup defeats the purpose). Documented in .env.example.
BACKUP_HOST="${BABA_BACKUP_HOST:-$(envval BABA_BACKUP_HOST)}"; BACKUP_HOST="${BACKUP_HOST:-$SCRIPT_DIR/backups}"
KEEP="${BABA_BACKUP_KEEP:-$(envval BABA_BACKUP_KEEP)}"; KEEP="${KEEP:-14}"

log() { printf '%s baba-backup: %s\n' "$(date -u +%H:%M:%S)" "$*"; }
die() { printf '%s baba-backup ERROR: %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || die "docker not found"
docker inspect "$PG_CONTAINER" >/dev/null 2>&1 || die "container $PG_CONTAINER not running"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$BACKUP_HOST"
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/baba-backup.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT

# track_embedding_samples is the raw per-sample re-ID history — tens of GB and
# growing unbounded (no retention), NOT irreplaceable config. The canonical
# per-track/identity embeddings live in `tracks` + `identity_labels` (small,
# fully dumped). We keep the table SCHEMA but drop its DATA so the archive stays
# small + fast; re-ID rebuilds its sample history over time after a restore.
# Override the excluded tables with BABA_BACKUP_EXCLUDE_DATA (comma-separated).
EXCLUDE_DATA="${BABA_BACKUP_EXCLUDE_DATA:-$(envval BABA_BACKUP_EXCLUDE_DATA)}"
EXCLUDE_DATA="${EXCLUDE_DATA:-track_embedding_samples}"
exclude_args=()
IFS=',' read -ra _ex <<< "$EXCLUDE_DATA"
for t in "${_ex[@]}"; do
    t="$(echo "$t" | tr -d '[:space:]')"
    [[ -n "$t" ]] && exclude_args+=(--exclude-table-data="$t")
done

log "dumping Postgres ($POSTGRES_DB; data-excluded: ${EXCLUDE_DATA:-none}) …"
# -Fc = custom format → pg_restore --clean --if-exists on the way back.
docker exec -e PGPASSWORD="$POSTGRES_PASSWORD" "$PG_CONTAINER" \
    pg_dump -U "$POSTGRES_USER" -Fc "${exclude_args[@]}" "$POSTGRES_DB" > "$STAGE/db.dump" \
    || die "pg_dump failed"
DB_BYTES=$(stat -c%s "$STAGE/db.dump" 2>/dev/null || echo 0)

# A file of the right shape is not proof it is restorable, so the archive is
# READ — every data block decompressed, the SQL thrown away. The cheaper
# `pg_restore --list` is not enough: a custom-format archive keeps its table of
# contents near the FRONT, so a dump truncated halfway through the data lists
# perfectly. A full disk or a killed container produces exactly that dump, and
# this backup would have called it good and then pruned a real one to make room.
#
# No filename, so pg_restore reads stdin: naming /dev/stdin looks equivalent and
# is not — pg_restore reopens the path, cannot seek it, and fails with "did not
# find magic string in file header" on a perfectly good dump.
docker exec -i -e PGPASSWORD="$POSTGRES_PASSWORD" "$PG_CONTAINER" \
    pg_restore -f /dev/null < "$STAGE/db.dump" >/dev/null 2>&1 \
    || die "the dump is not a readable pg_dump archive — refusing to write a backup that cannot be restored"
log "dump verified readable ($DB_BYTES bytes)"

log "copying irreplaceable media (reference_photos, scene_crops) …"
mkdir -p "$STAGE/media"
media_dirs=()
for d in reference_photos scene_crops; do
    if [[ -d "$MEDIA_HOST/$d" ]]; then
        cp -a "$MEDIA_HOST/$d" "$STAGE/media/"
        media_dirs+=("$d")
    fi
done

# Secrets needed to make the archive STAND ALONE for full-host DR. Without
# these a restore onto a fresh host is stuck: POSTGRES_PASSWORD (in .env) is
# needed to load the dump, and BABA_SECRET_KEY / state/api/secret-key is the
# Fernet key that decrypts ai_settings provider keys in the dump — lose it and
# every encrypted row is permanently unreadable. Both are tiny. NOTE: this
# makes the archive able to DECRYPT the DB dump, so it is chmod 0600 below and
# MUST live on a trusted, access-controlled target.
have_secrets=0
if [[ -f "$ENV_FILE" ]]; then
    cp -a "$ENV_FILE" "$STAGE/env"
    have_secrets=1
fi
if [[ -d "$STATE_HOST/api" ]]; then
    cp -a "$STATE_HOST/api" "$STAGE/state_api"
    have_secrets=1
fi

# Highest applied migration = the schema version this dump was taken at. Used
# by restore.sh to refuse restoring a NEWER DB into OLDER code.
SCHEMA_VERSION=$(docker exec -e PGPASSWORD="$POSTGRES_PASSWORD" "$PG_CONTAINER" \
    psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc \
    "SELECT version FROM schema_versions ORDER BY version DESC LIMIT 1" 2>/dev/null | tr -d '[:space:]' || echo "unknown")
APP_REV=unknown
if [[ -f "$SCRIPT_DIR/revision.json" ]]; then
    APP_REV=$(sed -n 's/.*"version"[: ]*"\([^"]*\)".*/\1/p' "$SCRIPT_DIR/revision.json" | head -1)
    APP_REV="${APP_REV:-unknown}"
fi

cat > "$STAGE/manifest.json" <<EOF
{
  "created_utc": "$STAMP",
  "schema_version": "${SCHEMA_VERSION:-unknown}",
  "app_revision": "${APP_REV:-unknown}",
  "postgres_db": "$POSTGRES_DB",
  "db_dump_bytes": $DB_BYTES,
  "has_secrets": $([[ "$have_secrets" == 1 ]] && echo true || echo false),
  "media_dirs": [$(printf '"%s",' "${media_dirs[@]}" | sed 's/,$//')]
}
EOF

ARCHIVE="$BACKUP_HOST/baba-backup-$STAMP.tar.gz"
log "writing archive $ARCHIVE …"
tar_members=(manifest.json db.dump media)
[[ -f "$STAGE/env" ]] && tar_members+=(env)
[[ -d "$STAGE/state_api" ]] && tar_members+=(state_api)
# Create with a restrictive umask so the archive is never group/world-readable
# even for an instant — it now carries the keys that decrypt the DB dump.
( umask 077; tar -C "$STAGE" -czf "$ARCHIVE.tmp" "${tar_members[@]}" )
chmod 600 "$ARCHIVE.tmp"
mv "$ARCHIVE.tmp" "$ARCHIVE"
# `ls -lh`, not `du -h`: du reports ALLOCATED BLOCKS, and on a network share a
# freshly written archive reports as "512" — a healthy run whose log reads like a
# 512-byte backup.
SIZE=$(ls -lh "$ARCHIVE" | awk '{print $5}')
log "done: $ARCHIVE ($SIZE, schema=$SCHEMA_VERSION, rev=$APP_REV, secrets=$have_secrets)"

# Retention: keep the newest $KEEP archives.
if [[ "$KEEP" =~ ^[0-9]+$ && "$KEEP" -gt 0 ]]; then
    mapfile -t old < <(ls -1t "$BACKUP_HOST"/baba-backup-*.tar.gz 2>/dev/null | tail -n +"$((KEEP+1))")
    for f in "${old[@]}"; do log "pruning old backup $(basename "$f")"; rm -f "$f"; done
fi
