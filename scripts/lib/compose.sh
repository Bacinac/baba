# shellcheck shell=bash
# docker compose orchestration helpers. Thin wrappers around the
# compose CLI so the wizard's stage code stays readable and the
# subprocess plumbing is in one place.

# Build only the base image that matches the chosen variant. The other
# bases stay un-built — a fresh nvidia install shouldn't waste minutes
# (and ~10 GB of layer cache) on a cpu base it'll never run.
compose_build_base() {
    local variant="$1"
    case "$variant" in
        cpu|nvidia|intel) ;;
        *) c_err "Unknown variant: $variant"; return 1;;
    esac
    c_step "Building baba/base:${variant}…"
    # --pull keeps the upstream base (python:3.14-slim / nvidia/cuda
    # / openvino/ubuntu24) fresh on re-runs. Without it a stale local
    # base image silently bakes old CVEs into every BABA service.
    docker compose --profile bases build --pull "base-${variant}"
    c_ok "baba/base:${variant} built"
}

# Build every service image. The compose file picks the right Dockerfile
# target per service based on BABA_VARIANT (with optional per-service
# override). buildkit's `--parallel` is implicit in compose v2.
compose_build_services() {
    c_step "Building service images…"
    docker compose build
    c_ok "Service images built"
}

# Pull the prebuilt service + infra images from the registry (GHCR by
# default — see the image-naming note in docker-compose.yml). Bases are
# profile-gated so they're skipped. Returns nonzero if any image can't
# be pulled (offline, registry down, packages private) — callers decide
# whether to fall back to a local build.
compose_pull_images() {
    c_step "Pulling prebuilt images (${BABA_IMAGE_BASE:-ghcr.io/bacinac/baba})…"
    if docker compose pull; then
        c_ok "Images pulled"
        return 0
    fi
    return 1
}

# `docker compose up -d` with the chosen override files merged in.
# Accepts an array of override files in $@ (e.g. for the intel override).
compose_up() {
    local args=()
    local f
    for f in "$@"; do
        args+=(-f "$f")
    done
    c_step "Starting stack…"
    if (( ${#args[@]} > 0 )); then
        docker compose "${args[@]}" up -d --remove-orphans
    else
        docker compose up -d --remove-orphans
    fi
    c_ok "Stack started"
}

# Block until the api container reports healthy, or time out. Uses the
# compose service name (not container_name) so this works across renames.
compose_wait_api_healthy() {
    local timeout="${1:-120}"
    local start now status
    start=$(date +%s)
    c_step "Waiting for api healthcheck (up to ${timeout}s)…"
    while true; do
        # `docker inspect` is the standard way; compose ps' health
        # column is human-formatted ("Up 12s (healthy)") and brittle.
        status=$(docker inspect --format='{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' baba-api 2>/dev/null || echo "missing")
        case "$status" in
            healthy)
                c_ok "API healthy"
                return 0
                ;;
            unhealthy)
                c_err "API reports unhealthy. Check: docker compose logs api"
                return 1
                ;;
            none)
                c_warn "API container has no healthcheck — skipping wait"
                return 0
                ;;
            missing)
                c_dim "  api container not yet running…"
                ;;
            *)
                c_dim "  api: $status"
                ;;
        esac
        now=$(date +%s)
        if (( now - start > timeout )); then
            c_err "API didn't become healthy within ${timeout}s. Logs: docker compose logs api"
            return 1
        fi
        sleep 3
    done
}

# Run the model-download script in a one-shot container. Output goes
# straight to the operator's terminal so they see the per-file progress.
# On GPU variants (arg 2 nvidia/intel) also bake the .nv12.onnx detector
# variants so the *-nv12 decoder chain + nv12 model default actually have
# their file on disk; cpu decode emits RGB, so no bake there.
compose_download_models() {
    local tier="${1:-minimal}"   # minimal|full|skip
    local variant="${2:-}"       # nvidia|intel|cpu|empty
    local detector="${3:-}"      # GPU-tier detector basename (rf-detr-nano|d-fine-s|…)
    if [[ -z "$detector" && -n "${BABA_DETECTOR_MODEL:-}" ]]; then
        detector="${BABA_DETECTOR_MODEL##*/}"
        detector="${detector%.onnx}"
        detector="${detector%.nv12}"
    fi
    local -a extra=()
    if [[ -n "$variant" && "$variant" != "cpu" ]]; then
        extra+=(--bake-nv12)
    fi
    # Fetch the detector install.sh picked for this GPU tier; rf-detr is in its
    # own bucket so --bake-nv12 correctly skips it (it rides the plain .onnx).
    [[ -n "$detector" ]] && extra+=(--detector "$detector")
    if [[ "$tier" == "skip" ]]; then
        c_dim "  Skipping model download."
        return 0
    fi
    if [[ ! -x ./tools/download_models.sh ]]; then
        c_warn "tools/download_models.sh missing or not executable — skipping."
        return 0
    fi
    c_step "Downloading models ($tier)…"
    case "$tier" in
        minimal)
            # RT-DETRv2-R18 + DINOv2-S + face stack. ~430 MB.
            ./tools/download_models.sh --minimal "${extra[@]}"
            ;;
        full)
            # Full zoo — every detector size + both DINOv2 sizes + face.
            # ~3 GB; useful when the operator wants to swap models in
            # the UI without re-downloading.
            ./tools/download_models.sh "${extra[@]}"
            ;;
        *)
            c_warn "Unknown tier '$tier' — defaulting to minimal."
            ./tools/download_models.sh --minimal "${extra[@]}"
            ;;
    esac
}
