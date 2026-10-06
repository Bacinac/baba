#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
image=busybox:1.37
prefix="baba-test-storage-$$"
volumes=()
mounts=()
cleanup() {
    for volume in "${volumes[@]}"; do
        docker volume rm "$volume" >/dev/null
    done
}
trap cleanup EXIT
trap 'exit 130' INT TERM
for path in models state media shm; do
    volume="$prefix-$path"
    docker volume create "$volume" >/dev/null
    volumes+=("$volume")
    target="/$path"
    [[ "$path" != shm ]] || target=/dev/shm
    mounts+=(--mount "type=volume,src=$volume,dst=$target,volume-nocopy")
done

docker run --rm "${mounts[@]}" "$image" sh -eu -c '
    mkdir /media/existing
    chmod 000 /media/existing
    test "$(stat -c %u /models)" = 0
'
for pass in first repeat; do
    docker run --rm "${mounts[@]}" -v "$ROOT/scripts/init-storage.sh:/init-storage.sh:ro" \
        -e BABA_UID=1234 -e BABA_GID=2345 "$image" sh /init-storage.sh
    docker run --rm --user 1234:2345 "${mounts[@]}" "$image" sh -eu -c '
        for path in /models /models/cache /models/cache/openvino /state /media /dev/shm; do
            touch "$path/probe"
            rm "$path/probe"
        done
        test "$(stat -c %u /media/existing)" = 0
        test "$(stat -c %a /media/existing)" = 0
        test "$(stat -c %a /dev/shm)" = 1777
    '
    echo "storage-init: $pass run writable by configured runtime user"
done

if docker run --rm -v "$prefix-models:/models:ro" \
    -v "$ROOT/scripts/init-storage.sh:/init-storage.sh:ro" \
    -e BABA_UID=3456 -e BABA_GID=4567 "$image" sh /init-storage.sh; then
    echo "storage-init: read-only storage incorrectly reported success" >&2
    exit 1
fi
echo "storage-init: read-only storage fails before consumers start"
