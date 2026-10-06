#!/bin/sh
set -eu

owner="${BABA_UID:-1000}:${BABA_GID:-1000}"
mkdir -p /models/cache/openvino /state /media
for path in /models /models/cache /models/cache/openvino /state /media; do
    if [ "$(stat -c '%u:%g' "$path")" != "$owner" ]; then
        chown "$owner" "$path"
    fi
    mode=$(stat -c '%a' "$path")
    if [ "$((0$mode & 0700))" -ne 448 ]; then
        chmod u+rwx "$path"
    fi
done
chmod 1777 /dev/shm
