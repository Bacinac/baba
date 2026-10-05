#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK=$(mktemp -d /tmp/baba-restore-test.XXXXXX)
PROJECT="baba-restore-test-$$"
PG_CONTAINER="$PROJECT-pg"
PG_IMAGE=${BABA_TEST_PG_IMAGE:-$(sed -n 's|^ *image: *\(pgvector/pgvector:[^ ]*\).*|\1|p' "$ROOT/docker-compose.yml" | head -1)}
export COMPOSE_FILE="$WORK/compose.yml" COMPOSE_PROJECT_NAME="$PROJECT"
compose=(docker compose --project-directory "$ROOT")
cleanup() {
    result=$?
    if (( result != 0 )); then
        latest_log=$(ls -1t "$WORK"/*.log 2>/dev/null | head -1 || true)
        [[ -z "$latest_log" ]] || tail -n 30 "$latest_log" >&2
    fi
    "${compose[@]}" down -v >/dev/null 2>&1 || true
    rm -rf "$WORK"
}
trap cleanup EXIT
cat > "$COMPOSE_FILE" <<EOF
services:
  postgres:
    image: $PG_IMAGE
    container_name: $PG_CONTAINER
    environment:
      POSTGRES_USER: baba
      POSTGRES_PASSWORD: restore-test-password
      POSTGRES_DB: baba
    tmpfs: [/var/lib/postgresql]
    healthcheck:
      test: [CMD-SHELL, pg_isready -U baba -d baba]
      interval: 1s
      timeout: 3s
      retries: 60
    networks: [isolated]
  sentinel:
    image: $PG_IMAGE
    container_name: $PROJECT-sentinel
    entrypoint: [sh, -c, "trap 'exit 0' TERM; while :; do sleep 1; done"]
    depends_on:
      postgres: {condition: service_healthy}
    networks: [isolated]
networks:
  isolated: {internal: true}
EOF
mkdir -p "$WORK/media/reference_photos" "$WORK/media/scene_crops" "$WORK/state/api" "$WORK/archives"
printf 'reference fixture\n' > "$WORK/media/reference_photos/fixture"
printf 'scene fixture\n' > "$WORK/media/scene_crops/fixture"
printf 'restore-test-signing-key\n' > "$WORK/state/api/secret-key"
printf 'POSTGRES_PASSWORD=restore-test-password\n' > "$WORK/env"
chmod 600 "$WORK/env" "$WORK/state/api/secret-key"
export BABA_PG_CONTAINER="$PG_CONTAINER" POSTGRES_USER=baba POSTGRES_DB=baba POSTGRES_PASSWORD=restore-test-password
export BABA_ENV_FILE="$WORK/env" BABA_MEDIA_HOST="$WORK/media" BABA_STATE_HOST="$WORK/state" BABA_BACKUP_HOST="$WORK/archives"
"${compose[@]}" up -d >/dev/null 2>&1
sql() { docker exec "$PG_CONTAINER" psql -v ON_ERROR_STOP=1 -U baba -d baba -Atc "$1"; }
CODE_VER=$(find "$ROOT/db/migrations" -maxdepth 1 -name '*.sql' -printf '%f\n' | sort | tail -1)
CODE_VER=${CODE_VER%.sql}
sql "CREATE TABLE schema_versions(version text PRIMARY KEY);
     INSERT INTO schema_versions VALUES ('$CODE_VER');
     CREATE TABLE recovery_labels(id integer PRIMARY KEY, value text NOT NULL);
     CREATE TABLE recovery_tracks(id integer PRIMARY KEY, label_id integer REFERENCES recovery_labels(id));
     INSERT INTO recovery_labels SELECT i,md5(i::text)||md5((i+1)::text) FROM generate_series(1,5000) i;
     INSERT INTO recovery_tracks SELECT i,i FROM generate_series(1,5000) i;" >/dev/null
fingerprint() {
    sql "SELECT count(*),md5(string_agg(id||value,'' ORDER BY id)) FROM recovery_labels;
         SELECT count(*),sum(id),sum(label_id) FROM recovery_tracks;
         SELECT count(*) FROM pg_constraint WHERE connamespace='public'::regnamespace;"
}
fingerprint > "$WORK/before"
bash "$ROOT/scripts/backup.sh" > "$WORK/backup.log" 2>&1
ARCHIVE=$(find "$WORK/archives" -name 'baba-backup-*.tar.gz' -print)
mkdir "$WORK/corrupt"
tar -xzf "$ARCHIVE" -C "$WORK/corrupt"
bytes=$(stat -c%s "$WORK/corrupt/db.dump")
head -c "$((bytes/2))" "$WORK/corrupt/db.dump" > "$WORK/truncated.dump"
cp "$WORK/truncated.dump" "$WORK/corrupt/db.dump"
tar -czf "$WORK/corrupt.tar.gz" -C "$WORK/corrupt" .
docker exec -i "$PG_CONTAINER" pg_restore --list < "$WORK/truncated.dump" >/dev/null
sentinel_started=$(docker inspect -f '{{.State.StartedAt}}' "$PROJECT-sentinel")
postgres_started=$(docker inspect -f '{{.State.StartedAt}}' "$PG_CONTAINER")
if bash "$ROOT/scripts/restore.sh" "$WORK/corrupt.tar.gz" --yes > "$WORK/corrupt.log" 2>&1; then
    echo 'corrupt archive was accepted' >&2; exit 1
fi
grep -q 'not fully readable' "$WORK/corrupt.log"
fingerprint > "$WORK/after"
cmp "$WORK/before" "$WORK/after"
[[ "$(docker inspect -f '{{.State.StartedAt}}' "$PROJECT-sentinel")" == "$sentinel_started" ]]
[[ "$(docker inspect -f '{{.State.Running}}' "$PROJECT-sentinel")" == true ]]
if COMPOSE_PROJECT_NAME="$PROJECT-other" bash "$ROOT/scripts/restore.sh" "$ARCHIVE" --yes > "$WORK/project.log" 2>&1; then
    echo 'unrelated Compose project was accepted' >&2; exit 1
fi
grep -q 'not this Compose' "$WORK/project.log"
fingerprint > "$WORK/after"
cmp "$WORK/before" "$WORK/after"
[[ "$(docker inspect -f '{{.State.StartedAt}}' "$PROJECT-sentinel")" == "$sentinel_started" ]]
mkdir "$WORK/unsupported"
tar -xzf "$ARCHIVE" -C "$WORK/unsupported"
sed -i 's/.*"schema_version".*/  "schema_version": "unknown",/' "$WORK/unsupported/manifest.json"
tar -czf "$WORK/unknown-schema.tar.gz" -C "$WORK/unsupported" .
if bash "$ROOT/scripts/restore.sh" "$WORK/unknown-schema.tar.gz" --yes > "$WORK/schema.log" 2>&1; then
    echo 'unknown archive schema was accepted' >&2; exit 1
fi
grep -q 'no known schema version' "$WORK/schema.log"
fingerprint > "$WORK/after"
cmp "$WORK/before" "$WORK/after"
[[ "$(docker inspect -f '{{.State.StartedAt}}' "$PROJECT-sentinel")" == "$sentinel_started" ]]
mkdir "$WORK/docker-shim"
export BABA_TEST_DOCKER_BINARY
BABA_TEST_DOCKER_BINARY=$(command -v docker)
cat > "$WORK/docker-shim/docker" <<'EOF'
#!/usr/bin/env bash
if [[ "${1:-}" == compose ]]; then
    for arg in "$@"; do [[ "$arg" != stop ]] || exit 1; done
fi
exec "$BABA_TEST_DOCKER_BINARY" "$@"
EOF
chmod 755 "$WORK/docker-shim/docker"
if PATH="$WORK/docker-shim:$PATH" bash "$ROOT/scripts/restore.sh" "$ARCHIVE" --yes > "$WORK/stop.log" 2>&1; then
    echo 'service stop failure was accepted' >&2; exit 1
fi
grep -q 'could not stop all app services' "$WORK/stop.log"
fingerprint > "$WORK/after"
cmp "$WORK/before" "$WORK/after"
[[ "$(docker inspect -f '{{.State.Running}}' "$PROJECT-sentinel")" == true ]]
sql 'CREATE TABLE recovery_guard(label_id integer REFERENCES recovery_labels(id));' >/dev/null
fingerprint > "$WORK/guard-before"
if bash "$ROOT/scripts/restore.sh" "$ARCHIVE" --yes > "$WORK/sql-error.log" 2>&1; then
    echo 'SQL dependency failure was accepted' >&2; exit 1
fi
grep -q 'pg_restore FAILED (single transaction)' "$WORK/sql-error.log"
fingerprint > "$WORK/guard-after"
cmp "$WORK/guard-before" "$WORK/guard-after"
[[ "$(docker inspect -f '{{.State.Running}}' "$PROJECT-sentinel")" == false ]]
[[ "$(docker inspect -f '{{.State.StartedAt}}' "$PG_CONTAINER")" == "$postgres_started" ]]
sql 'DROP TABLE recovery_guard; INSERT INTO recovery_labels VALUES (5001, '\''changed'\'');' >/dev/null
printf 'changed\n' > "$WORK/media/reference_photos/fixture"
export BABA_STATE_HOST="$WORK/restored-state"
bash "$ROOT/scripts/restore.sh" "$ARCHIVE" --yes > "$WORK/valid.log" 2>&1
fingerprint > "$WORK/after"
cmp "$WORK/before" "$WORK/after"
printf 'reference fixture\n' | cmp - "$WORK/media/reference_photos/fixture"
printf 'scene fixture\n' | cmp - "$WORK/media/scene_crops/fixture"
cmp "$WORK/state/api/secret-key" "$WORK/restored-state/api/secret-key"
[[ "$(stat -c%a "$WORK/restored-state/api/secret-key")" == 600 ]]
[[ "$(docker inspect -f '{{.State.Running}}' "$PROJECT-sentinel")" == true ]]
[[ "$(docker inspect -f '{{.State.StartedAt}}' "$PG_CONTAINER")" == "$postgres_started" ]]
echo 'backup/restore: corrupt archive, project/schema boundaries, stop failure, SQL rollback and populated round trip passed'
