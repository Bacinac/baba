#!/bin/sh
# Assembles the server config from the environment, then becomes nats-server.
#
# Clients log in with the plaintext passwords from .env; the server is given only
# their bcrypt hashes. A hash goes into the environment quoted because
# nats-server parses a substituted environment value as config, where a bare
# `$2y$…` would read as a variable reference.
set -eu -o pipefail

conf=/etc/nats/run.conf
echo 'include "nats.conf"' > "$conf"

if [ -n "${BABA_NATS_WEBSOCKET:-}" ]; then
    if ! err=$(openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 3650 \
            -subj /CN=baba-nats -keyout /etc/nats/ws.key -out /etc/nats/ws.crt 2>&1); then
        echo "$err" >&2
        exit 1
    fi
    echo 'include "nats-ws.conf"' >> "$conf"
fi

# The bus always runs with accounts. It carries every camera's state, and on a
# host that publishes it the mirror's listener faces the LAN or a tunnel. An
# unset password does not disable its account, it makes it accept an EMPTY one
# (proven, not assumed), so a missing credential stops the server instead.
for var in "NATS_USER=${NATS_USER:-}" "NATS_PASSWORD=${NATS_PASSWORD:-}" \
           "BABA_PEER_NATS_PASSWORD=${BABA_PEER_NATS_PASSWORD:-}"; do
    if [ -z "${var#*=}" ]; then
        echo "${var%%=*} is not set; run ./install.sh --upgrade to generate the NATS credentials" >&2
        exit 1
    fi
done
bcrypt() { printf '%s' "$1" | htpasswd -niBC 11 _ | cut -d: -f2-; }
NATS_PASSWORD_BCRYPT="\"$(bcrypt "$NATS_PASSWORD")\""
BABA_PEER_NATS_PASSWORD_BCRYPT="\"$(bcrypt "$BABA_PEER_NATS_PASSWORD")\""
export NATS_PASSWORD_BCRYPT BABA_PEER_NATS_PASSWORD_BCRYPT
unset NATS_PASSWORD BABA_PEER_NATS_PASSWORD
echo 'include "nats-auth.conf"' >> "$conf"

exec nats-server -c "$conf"
