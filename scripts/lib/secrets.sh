# shellcheck shell=bash
# Secret generators. All use python3's `secrets` module — fast, in
# the stdlib, cryptographically appropriate. Bash + /dev/urandom would
# work but the python3 stdlib path saves the base64 encoding step.

# 64 hex chars = 32 random bytes. Matches what auth.py persists to
# state/api/secret-key when BABA_SECRET_KEY isn't set in env.
secrets_jwt_key() {
    python3 -c 'import secrets; print(secrets.token_hex(32))'
}

# 32 URL-safe chars (24 random bytes). Lots for a service-to-service credential
# that never gets typed by a human. The dash + underscore in the alphabet are
# safe in the URL we embed it into (`nats://baba:<pass>@nats:4222`); the bus
# itself only ever sees its bcrypt hash (docker/nats-start.sh).
secrets_nats_password() {
    python3 -c 'import secrets; print(secrets.token_urlsafe(24))'
}
