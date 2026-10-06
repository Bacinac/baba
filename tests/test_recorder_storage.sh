#!/usr/bin/env bash
set -euo pipefail
umask 077
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NAME="baba-storage-test-$$"
FIXTURE="$(mktemp -d /tmp/baba-storage-test.XXXXXX)"
IMAGE="${BABA_STORAGE_TEST_IMAGE:-ghcr.io/bacinac/baba/recorder:cpu}"
PG_IMAGE="${BABA_TEST_PG_IMAGE:-postgres:18}"
cleanup() {
    docker stop "$NAME-runner" "$NAME-pg" >/dev/null 2>&1 || true
    docker rm "$NAME-runner" "$NAME-pg" >/dev/null 2>&1 || true
    docker network rm "$NAME" >/dev/null 2>&1 || true
    rm -r "$FIXTURE"
}
trap cleanup EXIT
mkdir -p "$FIXTURE/segments/fault"
touch "$FIXTURE/segments/fault/retention.mp4"
chmod -R a+rX "$FIXTURE"
docker network create --internal "$NAME" >/dev/null
docker run -d --name "$NAME-pg" --network "$NAME" --network-alias postgres \
    --memory 512m --tmpfs /var/lib/postgresql:size=256m \
    -e POSTGRES_PASSWORD=storage_fixture -e POSTGRES_DB=storage "$PG_IMAGE" >/dev/null
for attempt in {1..30}; do
    if docker exec -e PGPASSWORD=storage_fixture "$NAME-pg" psql -h 127.0.0.1 -U postgres -d storage -Atc 'SELECT 1' >/dev/null 2>&1; then break; fi
    sleep 1
done
docker exec -e PGPASSWORD=storage_fixture "$NAME-pg" psql -h 127.0.0.1 -U postgres -d storage -Atc 'SELECT 1' >/dev/null
docker run --name "$NAME-runner" --network "$NAME" --memory 512m --user 1000 \
    --tmpfs /fault:size=1m,mode=0777 --tmpfs /recovery:size=8m,mode=0777 \
    --tmpfs /recovery2:size=8m,mode=0777 --tmpfs /shutdown:size=8m,mode=0777 -v "$FIXTURE:/ro:ro" -v "$ROOT:/w:ro" \
    -e PYTHONPATH=/w/core/src:/w/services/recorder/src --entrypoint python "$IMAGE" \
    /w/tests/recorder_storage_probe.py
