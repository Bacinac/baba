#!/usr/bin/env sh
# BABA test gate — pytest inside a service image (containers only; nothing is
# ever pip-installed on the host).
#
#   sh tests/run.sh
#
# The image supplies the DEPENDENCIES; OUR packages resolve from the tree this
# script sits in via PYTHONPATH, ahead of the copies baked into the image — so
# the gate tests that source with no rebuild. The pre-push hook runs it from an
# export of each commit being pushed, so what passes is what ships. pytest and ruff are
# installed at test time into the container, pinned by uv.lock's `test` and
# `lint` groups, and never into a production image (a couple of seconds; the
# container runs unprivileged, so no shared cache).
#
# The event-manager image is the runner because it carries the dependencies
# every testable module here needs (numpy, msgspec); any locally built variant
# (intel/nvidia/cpu) will do. No image is a hard failure, not a skip — a gate
# that silently passes when it cannot run is worse than no gate.
#
# `baba_embedder` too, for the same reason: the rule that decides which faces
# are worth re-reading from the recording lives there, and it imports nothing
# the runner does not already carry.
#
# `baba_tracker` is on the path too. It is not the runner's own package, but
# the anchor hold — the mechanism that carries a seated person's presence —
# lives there, and it was untestable purely because this line was short. Its
# pure rules import cleanly against the same dependencies.
#
# And `baba_recorder` and `baba_ingestor`: which of a camera's streams is
# recorded and which is analysed is decided there, and the recorder decides
# which of ffmpeg's complaints are worth a warning.
#
# What retention deletes can only be told apart in real SQL, so those tests
# run against a throwaway Postgres: the image docker-compose.yml runs (read
# from there, not written twice), on a network of its own, removed on exit. A
# run killed past its trap leaves them behind; the next run reaps any whose
# pid is gone.
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Supply-chain gate, before the tests: a leaked secret or a known-vulnerable
# dependency must stop a push, not be found after it. Both tools run in their
# own images (containers only, nothing on the host). A missing image is a loud
# failure like the test images — a scanner that silently no-ops is worse than
# none. Opt out of a single run with BABA_SKIP_SECURITY_SCAN=1 (kept for the
# rare offline machine).
if [ "${BABA_SKIP_SECURITY_SCAN:-0}" != "1" ]; then
    # gitleaks is offline and deterministic. Scan the tree, not the history:
    # the gate guards the commit about to be pushed; history cleanup is its own
    # task (filter-repo). A hit fails the run.
    echo "gate: gitleaks (secret scan)…"
    docker run --rm -v "$ROOT:/w:ro" ghcr.io/gitleaks/gitleaks:latest \
        dir --no-banner --redact --config /w/.gitleaks.toml /w \
        || { echo "tests/run.sh: gitleaks found a secret — fix before pushing" >&2; exit 1; }

    # osv-scanner reads the manifests/lockfiles and reports known CVEs. A
    # non-zero exit means a vulnerable dependency (or no manifest at all is a
    # zero exit); either way a found vuln stops the push.
    echo "gate: osv-scanner (dependency CVEs)…"
    docker run --rm -v "$ROOT:/w:ro" ghcr.io/google/osv-scanner:latest \
        scan --recursive /w \
        || { echo "tests/run.sh: osv-scanner found a vulnerable dependency" >&2; exit 1; }
fi

# A missing image is a hard failure, not a skip.
local_image() {
    img=$(docker images --format '{{.Repository}}:{{.Tag}}' \
          | grep -E "^ghcr\.io/bacinac/baba/$1:($2)\$" | head -1)
    if [ -z "$img" ]; then
        echo "tests/run.sh: no local baba $1 image — build one first" >&2
        echo "  (docker compose build $1, or run a deploy once)" >&2
        exit 1
    fi
    echo "$img"
}

# One image per dependency set, because no one image carries them all: the
# event-manager has httpx and msgspec but not norfair, the tracker has norfair
# but not httpx, and only the api has fastapi. The tracker went 3990 lines with
# a single test import for exactly this reason, and the 2026-09-06 audit found
# eleven defects in it.
#
# Everything not listed here runs in the default image. Every file under
# tests/ therefore runs in one pass or another — a new one cannot fall through
# the gap, it simply runs in the default image unless it is named below.
TRACKER_TESTS="test_tracker_audit.py test_tracker_association.py test_anchor_hold.py test_tracker_service.py"
API_TESTS="test_role_gate.py test_login_limit.py test_camera_streams_api.py test_audit_vocabulary.py test_incident_changes.py test_sightings_feed.py"
PG_TESTS="test_recording_retention.py test_recording_health.py test_tracks_retention.py test_camera_streams.py test_finalize.py test_observe.py test_scene_unsure.py test_review_event_manager.py"
API_PG_TESTS="test_face_model_space.py test_reference_photo_enrolment.py test_face_recompute.py test_review_fixes.py"

IMG=$(local_image event-manager 'intel|nvidia|cpu') || exit 1
TIMG=$(local_image tracker 'intel|nvidia|cpu') || exit 1
AIMG=$(local_image api 'intel|nvidia|cpu') || exit 1
# The web runs against the tree its lockfile names, not whatever web image was
# built last: the Dockerfile's deps stage IS that tree, and the layer cache
# rebuilds it only when package.json or the lock changes.
WIMG=$(docker build -q --target deps -f "$ROOT/web/Dockerfile" "$ROOT") \
    || { echo "tests/run.sh: the web's dependencies do not install" >&2; exit 1; }

# The tools a pass needs, from uv.lock's dependency groups, hash-checked.
tools() {
    groups=""
    for g in "$@"; do groups="$groups --only-group $g"; done
    echo "uv export -q --frozen --directory /w --no-emit-workspace $groups -o /tmp/tools.txt \
          && uv pip install -q --python /opt/venv/bin/python3 --target /tmp/tools --require-hashes -r /tmp/tools.txt"
}

# Every image installs from uv.lock with --frozen, which trusts the lock as
# written: a pyproject edited without `uv lock` would build from the stale
# lock without a word. The lock check refuses that, and ruff is the lint gate
# the root pyproject declares.
echo "gate: uv lock --check, ruff…"
docker run --rm --entrypoint sh -v "$ROOT:/w:ro" -e UV_CACHE_DIR=/tmp/uv-cache "$IMG" -c \
    "uv lock --check --offline --directory /w && $(tools lint) && cd /w && /tmp/tools/bin/ruff check --no-cache --quiet ." \
    || { echo "tests/run.sh: uv.lock is stale (run uv lock) or ruff found an error" >&2; exit 1; }

# Every task has an owner: `home_core.tasks.spawn` holds the reference the
# event loop does not (it keeps only a weak one, so an unowned task can be
# collected mid-flight) and logs the failure a task nobody awaits would take
# with it. A bare create_task is the way back to both.
echo "gate: no bare create_task…"
bare=$(grep -rn --include='*.py' 'create_task(' \
        "$ROOT/core" "$ROOT/backends" "$ROOT/services" "$ROOT/tools" "$ROOT/demo" \
        | grep -v -e '/tests/' -e '/home_core/tasks\.py:' || true)
if [ -n "$bare" ]; then
    echo "$bare" >&2
    echo "tests/run.sh: a bare create_task — start it with home_core.tasks.spawn" >&2
    exit 1
fi

# Every service imports baba_core's package init, so whatever that init imports
# must be baba-core's own dependency. One that only a base image carried left
# the doorbell and the recorder unable to start once nats-py left the base.
echo "gate: baba_core imports on its own dependencies…"
docker run --rm --entrypoint sh -v "$ROOT:/w:ro" -e UV_CACHE_DIR=/tmp/uv-cache "$IMG" -c \
    "uv venv -q /tmp/core --python /opt/venv/bin/python3 \
     && VIRTUAL_ENV=/tmp/core uv sync -q --frozen --no-editable --active --package baba-core --directory /w \
     && cd /tmp && /tmp/core/bin/python -c 'import baba_core'" \
    || { echo "tests/run.sh: baba_core imports a package baba-core does not declare" >&2; exit 1; }

PYPATH=/w/core/src:/w/backends/onnxruntime/src:/w/services/event-manager/src:/w/services/tracker/src:/w/services/api/src:/w/services/state-evaluator/src:/w/services/embedder/src:/w/services/recorder/src:/w/services/ingestor/src:/w/services/detector/src

for c in $(docker ps -aq --filter name=baba-test-pg-); do
    n=$(docker inspect -f '{{.Name}}' "$c"); [ -d "/proc/${n##*-}" ] || docker rm -f "$c" >/dev/null
done
for n in $(docker network ls --filter name=baba-test-net- --format '{{.Name}}'); do
    [ -d "/proc/${n##*-}" ] || docker network rm "$n" >/dev/null
done
PG_IMAGE=$(sed -n 's|^ *image: *\(pgvector/pgvector:[^ ]*\).*|\1|p' "$ROOT/docker-compose.yml" | head -1)
[ -n "$PG_IMAGE" ] || { echo "tests/run.sh: no postgres image in docker-compose.yml" >&2; exit 1; }
NET=baba-test-net-$$
PGC=baba-test-pg-$$
trap 'docker rm -f "$PGC" >/dev/null 2>&1; docker network rm "$NET" >/dev/null 2>&1' EXIT
trap 'exit 130' INT TERM
docker network create "$NET" >/dev/null
docker run -d --rm --name "$PGC" --network "$NET" --tmpfs /var/lib/postgresql \
    -e POSTGRES_USER=baba -e POSTGRES_PASSWORD=test -e POSTGRES_DB=baba \
    "$PG_IMAGE" -c fsync=off >/dev/null

run_in() {
    img=$1; opts=$2; shift 2
    docker run --rm --entrypoint sh $opts \
        -v "$ROOT:/w:ro" \
        -e UV_CACHE_DIR=/tmp/uv-cache \
        -e PYTHONPATH="$PYPATH" \
        -e BABA_GO2RTC_API_PASSWORD=test \
        "$img" -c "$(tools test) \
                   && PYTHONPATH=/tmp/tools:\$PYTHONPATH /opt/venv/bin/python3 -m pytest -q -p no:cacheprovider $*"
}

# Over TCP, not the socket: the image's init runs a socket-only server first
# and restarts it, and only the second one is the database the tests get.
pg_ready() {
    i=0
    until docker exec -e PGPASSWORD=test "$PGC" psql -h 127.0.0.1 -U baba -d baba -Atc 'SELECT 1' >/dev/null 2>&1; do
        i=$((i + 1))
        if [ "$i" -ge 120 ]; then
            echo "tests/run.sh: the test postgres never came up" >&2
            docker logs "$PGC" 2>&1 | tail -20 >&2
            exit 1
        fi
        sleep 0.5
    done
}

IGNORES=""
routed() {
    paths=""
    for t in $1; do
        [ -f "$ROOT/tests/$t" ] || continue
        IGNORES="$IGNORES --ignore=/w/tests/$t"
        paths="$paths /w/tests/$t"
    done
}
routed "$TRACKER_TESTS"; TRACKER_PATHS=$paths
routed "$API_TESTS"; API_PATHS=$paths
routed "$PG_TESTS"; PG_PATHS=$paths
routed "$API_PG_TESTS"; API_PG_PATHS=$paths

run_in "$IMG" "" "/w/tests $IGNORES"
[ -n "$TRACKER_PATHS" ] && run_in "$TIMG" "" "$TRACKER_PATHS"
# home_core is the api's too (argon2, JWT, FastAPI), so its tests run in that image.
run_in "$AIMG" "" "$API_PATHS /w/core/src/home_core/tests"
pg_ready
run_in "$IMG" "--network $NET -e BABA_TEST_DSN=postgresql://baba:test@$PGC/baba" "$PG_PATHS"
[ -n "$API_PG_PATHS" ] && run_in "$AIMG" "--network $NET -e BABA_TEST_DSN=postgresql://baba:test@$PGC/baba" "$API_PG_PATHS"
# The web server's own modules, under the Node that serves them.
docker run --rm -v "$ROOT:/w:ro" --entrypoint node "$WIMG" --test --test-reporter=dot '/w/tests/*.test.mjs'
# What the web says: every word in both languages, every control named, every
# text size a step of the kit's scale and every help article whole. The kit's
# checks resolve svelte/compiler beside the file, so they run from a copy
# inside the image rather than from a host node_modules.
docker run --rm -v "$ROOT:/w:ro" --entrypoint node "$WIMG" /w/web/src/lib/i18n/words.mjs
docker run --rm -v "$ROOT:/w:ro" --entrypoint sh "$WIMG" -c 'cp -r /w/web/src/lib/kit /app/kit \
    && node --test --test-reporter=dot /app/kit/*.test.mjs \
    && node /app/kit/names.mjs /w/web/src && node /app/kit/type.mjs /w/web/src \
    && node /app/kit/articles.mjs /w/web/src/lib/help /w/web/src'
# Types and unit tests, from a copy: svelte-kit sync writes into the project.
docker run --rm -e npm_config_update_notifier=false -v "$ROOT/web:/w:ro" --entrypoint sh "$WIMG" -c \
    'tar -C /w -c --exclude=./node_modules --exclude=./build --exclude=./demo-dist --exclude=./.svelte-kit . | tar -x -C /app \
     && npx svelte-kit sync && npx svelte-check --tsconfig ./tsconfig.json --output human --fail-on-warnings && npx vitest run'
