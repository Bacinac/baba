# shellcheck shell=bash
as_root() {
    if (( EUID == 0 )); then
        "$@"
    else
        sudo "$@"
    fi
}
# Sanity checks the host must pass before install can do anything
# useful. Fails fast with a clear message on each missing piece —
# better to halt at "install docker first" than to discover it three
# minutes into a build.

preflight_docker() {
    if ! command -v docker >/dev/null 2>&1; then
        c_err "docker not found on PATH."
        c_dim  "  Install: https://docs.docker.com/engine/install/"
        return 1
    fi
    if ! docker version >/dev/null 2>&1; then
        c_err "docker installed but not usable by this user."
        c_dim  "  Either run as root, add yourself to the docker group, or"
        c_dim  "  start the docker daemon: sudo systemctl start docker"
        return 1
    fi
    c_ok "docker $(docker version --format '{{.Server.Version}}' 2>/dev/null || echo '?')"
}

preflight_compose() {
    # We only support v2 (the plugin) — `docker-compose` v1 is EOL.
    if ! docker compose version >/dev/null 2>&1; then
        c_err "docker compose v2 plugin not available."
        c_dim  "  Install: https://docs.docker.com/compose/install/"
        return 1
    fi
    c_ok "docker compose $(docker compose version --short 2>/dev/null || echo '?')"
}

preflight_linux() {
    case "$(uname -s)" in
        Linux) c_ok "Linux $(uname -m)";;
        Darwin) c_warn "macOS host — BABA targets Linux. Some hardware passthrough (NVIDIA, /dev/dri) won't work.";;
        *) c_warn "$(uname -s) — untested host OS.";;
    esac
}

preflight_python() {
    # Only needed for secret generation. Bash + /dev/urandom would work
    # too but python3 is universally present on Linux distros + gives
    # us secrets.token_hex() which is the right call.
    if ! command -v python3 >/dev/null 2>&1; then
        c_err "python3 not found — needed to generate secrets."
        return 1
    fi
}

# Run everything; exit non-zero on any failure. Caller handles trap.
preflight_all() {
    c_step "Preflight…"
    preflight_docker
    preflight_compose
    preflight_linux
    preflight_python
}
