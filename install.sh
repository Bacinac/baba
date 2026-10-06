#!/usr/bin/env bash
# BABA — interactive installer / upgrader.
#
# First-install flow:
#   ./install.sh                    # full wizard
#
# Subsequent runs (existing .env detected):
#   ./install.sh                    # prompts: upgrade vs reconfigure vs cancel
#   ./install.sh --upgrade          # rebuild + restart, keep config
#   ./install.sh --reconfigure      # re-run wizard, apply answers to existing .env
#   ./install.sh --variant=cpu      # configuration flags imply --reconfigure;
#                                   # with --upgrade they are refused if they differ
#
# Non-interactive / CI / scripted:
#   ./install.sh --non-interactive \
#       --variant=nvidia --media-host=/srv/baba/media --admin=ops
#
# Other flags:
#   --skip-models                   # don't download model files
#   --models=minimal|full|skip      # set the model-download tier explicitly
#   --hostname=baba.example.com     # public hostname (sets CORS + COOKIE_SECURE)
#   --pull                          # force prebuilt images from the registry (no build)
#   --build                         # force local from-source build (no pull)
#                                   # default: pull first, fall back to build
#   --dry-run                       # show what would happen, write nothing
#   --help                          # this message
#
# Re-run safety: upgrade never changes an existing .env value; reconfigure
# changes only what the wizard asks about. New keys from a newer BABA
# version are appended. Secrets (BABA_SECRET_KEY, database, NATS, go2rtc)
# are generated exactly once and never rotated automatically (rotation
# invalidates every signed session).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$SCRIPT_DIR/scripts/lib"

# shellcheck source=scripts/lib/colors.sh
source "$LIB_DIR/colors.sh"
# shellcheck source=scripts/lib/prompt.sh
source "$LIB_DIR/prompt.sh"
# shellcheck source=scripts/lib/preflight.sh
source "$LIB_DIR/preflight.sh"
# shellcheck source=scripts/lib/secrets.sh
source "$LIB_DIR/secrets.sh"
# shellcheck source=scripts/lib/envgen.sh
source "$LIB_DIR/envgen.sh"
# shellcheck source=scripts/lib/compose.sh
source "$LIB_DIR/compose.sh"

# --- flag parsing ---------------------------------------------------------

NONINTERACTIVE=0
UPGRADE_MODE=0
RECONFIGURE_MODE=0
DRY_RUN=0
INSTALL_SOURCE="auto"   # auto = pull first, fall back to build | pull | build
FLAG_VARIANT=""
FLAG_MEDIA_HOST=""
FLAG_ADMIN=""
FLAG_HOSTNAME=""
FLAG_MODELS=""       # minimal | full | skip — empty = ask in wizard

print_help() {
    # Print the comment block at the top of this file (everything after
    # the shebang, up to the first non-comment line). Stripping a single
    # leading "# " makes the help output read like prose.
    awk '
        NR == 1            { next }                      # skip shebang
        /^#/               { sub(/^# ?/, ""); print; next }
        NR > 1 && !/^#/    { exit }
    ' "$0"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --non-interactive|--yes|-y) NONINTERACTIVE=1; shift;;
        --upgrade)        UPGRADE_MODE=1; shift;;
        --reconfigure)    RECONFIGURE_MODE=1; shift;;
        --pull)           INSTALL_SOURCE="pull"; shift;;
        --build)          INSTALL_SOURCE="build"; shift;;
        --dry-run)        DRY_RUN=1; shift;;
        --skip-models)    FLAG_MODELS="skip"; shift;;
        --models=*)       FLAG_MODELS="${1#*=}"; shift;;
        --variant=*)      FLAG_VARIANT="${1#*=}"; shift;;
        --media-host=*)   FLAG_MEDIA_HOST="${1#*=}"; shift;;
        --admin=*)        FLAG_ADMIN="${1#*=}"; shift;;
        --hostname=*)     FLAG_HOSTNAME="${1#*=}"; shift;;
        -h|--help)        print_help; exit 0;;
        *) c_err "Unknown flag: $1"; print_help; exit 1;;
    esac
done

# Trap so a half-way failure prints a useful pointer to the log file
# instead of the bare `set -e` exit. The user can read the log for
# what actually crashed (build output, healthcheck timeout, etc).
LOG_DIR="$SCRIPT_DIR"
LOG_FILE="$LOG_DIR/install.log"
ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }

on_error() {
    local rc=$?
    c_err "install.sh failed (exit $rc)."
    c_dim "  Full log: $LOG_FILE"
    exit "$rc"
}
trap on_error ERR

# Mirror stdout/stderr to a log file. tee with -a so re-runs append
# rather than nuking previous logs (which may have the clue you need).
exec > >(tee -a "$LOG_FILE") 2>&1
printf '\n=== %s install.sh start ===\n' "$(ts)"

# --- variant detection (shared helper, also used by scripts/detect-hw.sh) -

detect_variant() {
    if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
        echo "nvidia"
        return
    fi
    if command -v lspci >/dev/null 2>&1; then
        # Intel Arc / Xe discrete or iGPU on a kernel with i915. /dev/dri
        # presence is the second signal — without it OpenVINO falls back
        # to CPU anyway, but we still pick `intel` so the right base
        # image is built.
        if lspci 2>/dev/null | grep -qiE '(Intel.*(Arc|Xe|UHD|Iris)|VGA.*Intel)'; then
            echo "intel"
            return
        fi
    fi
    echo "cpu"
}

# Lookup the device name for the detected variant (for friendly output).
describe_variant() {
    case "$1" in
        nvidia)
            local name
            name=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)
            if [[ -n "$name" ]]; then
                # nvidia-smi already includes "NVIDIA" in most product
                # strings ("NVIDIA GeForce RTX 3060"); don't double it.
                [[ "$name" == NVIDIA* ]] && printf '%s' "$name" \
                                         || printf 'NVIDIA %s' "$name"
            else
                printf 'NVIDIA GPU'
            fi
            ;;
        intel)
            local name
            name=$(lspci 2>/dev/null | grep -iE '(Intel.*(Arc|Xe|UHD|Iris))' | head -1 | sed 's/^.*: //')
            [[ -n "$name" ]] && printf 'Intel %s' "$name" || printf 'Intel GPU'
            ;;
        cpu) printf 'CPU only (no GPU detected)';;
        *)   printf '%s' "$1";;
    esac
}

# Detect the host's `render` group gid. The intel compose override adds this
# as a supplementary group (group_add) so the uid-1000 service containers can
# open /dev/dri/renderD128 — without it OpenVINO enumerates CPU only and the
# require-GPU intel build hard-fails at load. Debian numbers render 993,
# Ubuntu 110; reading it from the host is the only reliable value. Falls back
# to the /dev/dri owner, then 110, for hosts where the group isn't named.
detect_render_gid() {
    local gid
    gid=$(getent group render 2>/dev/null | cut -d: -f3)
    if [[ -z "$gid" && -e /dev/dri/renderD128 ]]; then
        gid=$(stat -c %g /dev/dri/renderD128 2>/dev/null)
    fi
    printf '%s' "${gid:-110}"
}

# Classify the GPU into a capability tier so the detector default matches the
# hardware. A discrete GPU (NVIDIA, Intel Arc/Flex) has dedicated high-bandwidth
# VRAM and can run a ViT-backbone detector; a budget integrated GPU (Intel
# UHD/Iris, Alder Lake-N) shares system LPDDR5 and chokes on one — RF-DETR-nano
# measured ~3 fps + ~2 GB OOM on an N305 UHD vs ~24 fps on an Arc A380, because
# its DINOv2 backbone is memory-bandwidth-bound. Prints: dgpu-nvidia | dgpu-arc
# | igpu | cpu.
detect_gpu_tier() {
    case "$BABA_VARIANT" in
        nvidia) echo "dgpu-nvidia"; return ;;
        cpu)    echo "cpu"; return ;;
    esac
    # intel: discrete Arc/Flex vs integrated. Names from lspci: "Arc",
    # "Flex", "DG2", "Data Center GPU" are discrete; UHD/Iris/Alder Lake-N
    # are integrated.
    local name
    name=$(lspci 2>/dev/null | grep -iE 'VGA|Display|3D' | grep -i intel | head -1)
    if echo "$name" | grep -qiE '\bArc\b|\bFlex\b|DG2|Data Center GPU'; then
        echo "dgpu-arc"
    else
        echo "igpu"
    fi
}

# --- state detection ------------------------------------------------------

cd "$SCRIPT_DIR"
ENV_FILE="$SCRIPT_DIR/.env"

EXISTING_INSTALL=0
[[ -f "$ENV_FILE" ]] && EXISTING_INSTALL=1

# --- header ---------------------------------------------------------------

c_head "BABA — Onboarding wizard"
if (( EXISTING_INSTALL == 1 )); then
    c_dim "  Existing .env detected at $ENV_FILE"
fi
if (( DRY_RUN == 1 )); then
    c_warn "DRY RUN — no files will be written, no containers built or started."
fi

# --- preflight ------------------------------------------------------------

preflight_all

# --- existing-install branch logic ----------------------------------------

CONFIG_FLAGS=()
[[ -n "$FLAG_VARIANT" ]]    && CONFIG_FLAGS+=("--variant=$FLAG_VARIANT")
[[ -n "$FLAG_MEDIA_HOST" ]] && CONFIG_FLAGS+=("--media-host=$FLAG_MEDIA_HOST")
[[ -n "$FLAG_ADMIN" ]]      && CONFIG_FLAGS+=("--admin=$FLAG_ADMIN")
[[ -n "$FLAG_HOSTNAME" ]]   && CONFIG_FLAGS+=("--hostname=$FLAG_HOSTNAME")

# Each configuration flag against the existing .env, one line per value it
# would change. Empty output = the flags restate what is already configured.
config_flag_changes() {
    local cur
    if [[ -n "$FLAG_VARIANT" ]]; then
        cur=$(envgen_read "$ENV_FILE" BABA_VARIANT || echo "")
        [[ "$FLAG_VARIANT" != "$cur" ]] && echo "--variant=$FLAG_VARIANT differs from BABA_VARIANT=$cur"
    fi
    if [[ -n "$FLAG_MEDIA_HOST" ]]; then
        cur=$(envgen_read "$ENV_FILE" BABA_MEDIA_HOST || echo "")
        [[ "$FLAG_MEDIA_HOST" != "$cur" ]] && echo "--media-host=$FLAG_MEDIA_HOST differs from BABA_MEDIA_HOST=$cur"
    fi
    if [[ -n "$FLAG_ADMIN" ]]; then
        cur=$(envgen_read "$ENV_FILE" BABA_ADMIN_USERNAME || echo "")
        [[ "$FLAG_ADMIN" != "$cur" ]] && echo "--admin=$FLAG_ADMIN differs from BABA_ADMIN_USERNAME=$cur"
    fi
    if [[ -n "$FLAG_HOSTNAME" ]]; then
        cur=$(envgen_read "$ENV_FILE" BABA_CORS_ORIGINS || echo "")
        [[ "https://$FLAG_HOSTNAME" != "$cur" || "$(envgen_read "$ENV_FILE" BABA_COOKIE_SECURE || echo "")" != "true" ]] \
            && echo "--hostname=$FLAG_HOSTNAME differs from BABA_CORS_ORIGINS=$cur"
    fi
    return 0
}

if (( EXISTING_INSTALL == 1 && UPGRADE_MODE == 1 && ${#CONFIG_FLAGS[@]} > 0 )); then
    changes=$(config_flag_changes)
    if [[ -n "$changes" ]]; then
        c_err "--upgrade keeps the existing .env, but the configuration flags ask to change it:"
        while IFS= read -r line; do c_err "  $line"; done <<<"$changes"
        c_dim "  Drop --upgrade (or pass --reconfigure) to apply them."
        exit 1
    fi
fi

if (( EXISTING_INSTALL == 1 && UPGRADE_MODE == 0 && RECONFIGURE_MODE == 0 && ${#CONFIG_FLAGS[@]} > 0 )); then
    c_head "Existing installation"
    c_info "Configuration flags given (${CONFIG_FLAGS[*]}) — reconfiguring the existing .env with them."
    RECONFIGURE_MODE=1
fi

# The api creates the admin account once, when its users table is empty. With
# a database already on disk the account exists, and a new username in .env
# would change nothing at all.
if (( EXISTING_INSTALL == 1 && RECONFIGURE_MODE == 1 )) && [[ -n "$FLAG_ADMIN" ]]; then
    cur_admin=$(envgen_read "$ENV_FILE" BABA_ADMIN_USERNAME || echo "")
    state_dir="$(envgen_read "$ENV_FILE" BABA_STATE_HOST || echo "$SCRIPT_DIR/state")"
    # state/postgres holds a cluster from before Postgres 18 until the
    # postgres-upgrade service carries it into state/postgresql.
    pg_dir=""
    for d in "$state_dir/postgresql" "$state_dir/postgres"; do
        if [[ -n "$(ls -A "$d" 2>/dev/null)" ]]; then pg_dir=$d; break; fi
    done
    if [[ "$FLAG_ADMIN" != "$cur_admin" && -n "$pg_dir" ]]; then
        c_err "--admin=$FLAG_ADMIN differs from BABA_ADMIN_USERNAME=$cur_admin, but the database in $pg_dir"
        c_err "  already holds its users — the admin account is created only on the first boot."
        c_dim "  Add the user under Account in the web UI instead, and drop --admin."
        exit 1
    fi
fi

if (( EXISTING_INSTALL == 1 && UPGRADE_MODE == 0 && RECONFIGURE_MODE == 0 )); then
    c_head "Existing installation"
    c_info "An existing .env is in place. Choose:"
    pick=$(ask_choice "Action" 1 \
        "Upgrade (rebuild + restart, keep all config)" \
        "Reconfigure (re-run wizard, apply answers to existing .env)" \
        "Cancel")
    case "$pick" in
        1) UPGRADE_MODE=1;;
        2) RECONFIGURE_MODE=1;;
        3) c_info "Cancelled."; exit 0;;
    esac
fi

# --- wizard stages --------------------------------------------------------

# In upgrade mode we skip the wizard entirely — just append new keys
# (if any) and re-run build + up. In reconfigure / fresh install we run
# the full wizard. Defaults come from existing .env when present so
# the operator sees their last choices.

# Nothing runs without its credentials: the bus refuses to start without its
# accounts (docker/nats-start.sh), and go2rtc — network_mode:host, so its API
# and RTSP are on every host interface — would otherwise hand any LAN host
# every camera and, through /api/config, every camera's password. So every
# path through the installer ends with all of them set: the ones .env already
# has are kept, and only a missing one is generated. Rotating a kept one would
# lock out every connected service and mirror. The mirror's password is
# generated even before any mirror exists: its account lives in the same
# config, and without one the bus does not start at all.
ensure_credentials() {
    NATS_USER=$(envgen_read "$ENV_FILE" NATS_USER || echo "")
    NATS_PASSWORD=$(envgen_read "$ENV_FILE" NATS_PASSWORD || echo "")
    BABA_PEER_NATS_PASSWORD=$(envgen_read "$ENV_FILE" BABA_PEER_NATS_PASSWORD || echo "")
    BABA_GO2RTC_API_USER=$(envgen_read "$ENV_FILE" BABA_GO2RTC_API_USER || echo "")
    BABA_GO2RTC_API_PASSWORD=$(envgen_read "$ENV_FILE" BABA_GO2RTC_API_PASSWORD || echo "")
    CREDENTIALS_GENERATED=()
    [[ -n "$NATS_USER" ]] || { NATS_USER="baba"; CREDENTIALS_GENERATED+=(NATS_USER); }
    [[ -n "$NATS_PASSWORD" ]] || { NATS_PASSWORD=$(secrets_nats_password); CREDENTIALS_GENERATED+=(NATS_PASSWORD); }
    [[ -n "$BABA_PEER_NATS_PASSWORD" ]] \
        || { BABA_PEER_NATS_PASSWORD=$(secrets_nats_password); CREDENTIALS_GENERATED+=(BABA_PEER_NATS_PASSWORD); }
    [[ -n "$BABA_GO2RTC_API_USER" ]] \
        || { BABA_GO2RTC_API_USER="baba"; CREDENTIALS_GENERATED+=(BABA_GO2RTC_API_USER); }
    [[ -n "$BABA_GO2RTC_API_PASSWORD" ]] \
        || { BABA_GO2RTC_API_PASSWORD=$(secrets_nats_password); CREDENTIALS_GENERATED+=(BABA_GO2RTC_API_PASSWORD); }
    export NATS_USER NATS_PASSWORD BABA_PEER_NATS_PASSWORD BABA_GO2RTC_API_USER BABA_GO2RTC_API_PASSWORD
    if (( ${#CREDENTIALS_GENERATED[@]} > 0 )); then
        c_ok "Generated credentials: ${CREDENTIALS_GENERATED[*]}"
    else
        c_dim "  Keeping existing credentials."
    fi
}

run_wizard() {
    # ── Stage 1: hardware variant ──────────────────────────────────────
    c_head "1/6  Hardware"
    local detected
    detected=$(detect_variant)
    c_ok "Detected: $(describe_variant "$detected")"

    local existing_variant
    existing_variant=$(envgen_read "$ENV_FILE" BABA_VARIANT || echo "")

    if [[ -n "$FLAG_VARIANT" ]]; then
        BABA_VARIANT="$FLAG_VARIANT"
        c_dim "  Using --variant=$BABA_VARIANT"
    elif (( NONINTERACTIVE == 1 )) && [[ -n "$existing_variant" ]]; then
        BABA_VARIANT="$existing_variant"
        c_dim "  Non-interactive: keeping configured ($BABA_VARIANT)"
    elif (( NONINTERACTIVE == 1 )); then
        BABA_VARIANT="$detected"
        c_dim "  Non-interactive: using detected ($BABA_VARIANT)"
    else
        local idx
        case "${existing_variant:-$detected}" in
            nvidia) idx=1;; intel) idx=2;; *) idx=3;;
        esac
        local pick
        pick=$(ask_choice "Pick inference variant" "$idx" \
            "nvidia  — TensorRT/CUDA" \
            "intel   — OpenVINO (Arc/iGPU)" \
            "cpu     — ONNXRuntime (slow, dev only)")
        case "$pick" in
            1) BABA_VARIANT="nvidia";;
            2) BABA_VARIANT="intel";;
            3) BABA_VARIANT="cpu";;
        esac
    fi
    case "$BABA_VARIANT" in
        nvidia|intel|cpu) ;;
        *) c_err "Unknown variant: $BABA_VARIANT (nvidia | intel | cpu)"; exit 1;;
    esac
    # Everything derived from the variant (decoder, detector model, render
    # gid) is kept from .env only while the variant stays the same; the old
    # variant's values would not run on the new one.
    VARIANT_CHANGED=0
    if [[ -n "$existing_variant" && "$existing_variant" != "$BABA_VARIANT" ]]; then
        VARIANT_CHANGED=1
        c_warn "Variant changes $existing_variant → $BABA_VARIANT: decoder and detector model are re-derived."
    fi
    # The ingestor derives its decoder from the variant; BABA_DECODER_BACKEND
    # only names it for a host the default does not suit. An Intel iGPU gets
    # qsv-nv12: the N305's VAAPI path dies every ~60 s (vaSyncSurface) while
    # QSV on the same driver is stable, and the Arc runs VAAPI cleanly.
    BABA_DECODER_BACKEND=""
    if (( VARIANT_CHANGED == 0 )); then
        BABA_DECODER_BACKEND=$(envgen_read "$ENV_FILE" BABA_DECODER_BACKEND || echo "")
        if [[ -z "$BABA_DECODER_BACKEND" ]]; then
            BABA_DECODER_BACKEND=$(envgen_carried_decoder "$ENV_FILE")
        fi
    fi
    if [[ -z "$BABA_DECODER_BACKEND" && "$(detect_gpu_tier)" == "igpu" ]]; then
        BABA_DECODER_BACKEND="qsv-nv12"
    fi

    # ── Stage 2: storage paths ─────────────────────────────────────────
    c_head "2/6  Storage"
    local def_models def_state def_media
    def_models=$(envgen_read "$ENV_FILE" BABA_MODELS_HOST || echo "$SCRIPT_DIR/models")
    def_state=$(envgen_read "$ENV_FILE" BABA_STATE_HOST  || echo "$SCRIPT_DIR/state")
    def_media=$(envgen_read "$ENV_FILE" BABA_MEDIA_HOST  || echo "${FLAG_MEDIA_HOST:-$SCRIPT_DIR/media}")
    [[ -n "$FLAG_MEDIA_HOST" ]] && def_media="$FLAG_MEDIA_HOST"

    c_info "Fast tier (SSD/NVMe) — small, hot:"
    BABA_MODELS_HOST=$(ask_text "  Models" "$def_models")
    BABA_STATE_HOST=$(ask_text "  State (postgres)" "$def_state")
    c_info "Media tier — everything the cameras produce:"
    BABA_MEDIA_HOST=$(ask_text "  Media" "$def_media")
    local existing_media
    existing_media=$(envgen_read "$ENV_FILE" BABA_MEDIA_HOST || echo "")
    if [[ -n "$existing_media" && "$existing_media" != "$BABA_MEDIA_HOST" ]]; then
        c_warn "Media tier moves $existing_media → $BABA_MEDIA_HOST; recordings already under"
        c_warn "  $existing_media are not moved — the new tier starts empty."
    fi

    # Sanity-check media path: warn if it ends up on the same fs as
    # the rest of /mnt/docker (defeats the slow-tier separation), but
    # don't block — operator may genuinely want it for dev.
    if [[ "$BABA_MEDIA_HOST" == "$BABA_MODELS_HOST"* ]] || \
       [[ "$BABA_MEDIA_HOST" == "$SCRIPT_DIR"* ]]; then
        c_warn "Media path is under the fast tier — fine for dev, but on a"
        c_warn "real install set it to a dedicated HDD (TBs free space)."
    fi

    # ── Stage 3: admin account ─────────────────────────────────────────
    c_head "3/6  Admin account"
    local def_admin
    def_admin=$(envgen_read "$ENV_FILE" BABA_ADMIN_USERNAME || echo "${FLAG_ADMIN:-admin}")
    [[ -n "$FLAG_ADMIN" ]] && def_admin="$FLAG_ADMIN"
    BABA_ADMIN_USERNAME=$(ask_text "Username" "$def_admin")

    # Password strategy: default to "let api auto-generate and write
    # to state/api/bootstrap_admin_password" — that file is mode 0600
    # and the auth.py path handles it cleanly. Typing-now is an opt-in
    # for operators who insist on memorable passwords.
    BABA_ADMIN_PASSWORD=""
    if (( NONINTERACTIVE == 0 )); then
        local pwpick
        pwpick=$(ask_choice "Password" 1 \
            "Auto-generate (saved to state/api/bootstrap_admin_password)" \
            "Type my own")
        if [[ "$pwpick" == "2" ]]; then
            local p1 p2
            while true; do
                p1=$(ask_password "  New password")
                p2=$(ask_password "  Confirm")
                if [[ "$p1" != "$p2" ]]; then
                    c_warn "  Mismatch — try again."
                    continue
                fi
                if [[ ${#p1} -lt 8 ]]; then
                    c_warn "  Password must be at least 8 characters."
                    continue
                fi
                BABA_ADMIN_PASSWORD="$p1"
                break
            done
        fi
    fi

    # ── Stage 4: credentials ───────────────────────────────────────────
    c_head "4/6  Credentials"
    ensure_credentials

    # ── Stage 5: public access ─────────────────────────────────────────
    c_head "5/6  Public access"
    local existing_cors existing_secure
    existing_cors=$(envgen_read "$ENV_FILE" BABA_CORS_ORIGINS || echo "")
    existing_secure=$(envgen_read "$ENV_FILE" BABA_COOKIE_SECURE || echo "false")
    BABA_CORS_ORIGINS="$existing_cors"
    BABA_COOKIE_SECURE="$existing_secure"

    if [[ -n "$FLAG_HOSTNAME" ]]; then
        BABA_CORS_ORIGINS="https://${FLAG_HOSTNAME}"
        BABA_COOKIE_SECURE="true"
        c_dim "  Using --hostname=$FLAG_HOSTNAME → cors+secure cookie set."
    elif (( NONINTERACTIVE == 0 )); then
        if ask_yes_no "Will BABA be reachable on a public hostname?" no; then
            local host
            host=$(ask_text "  Hostname (no scheme)" "${BABA_CORS_ORIGINS#https://}")
            [[ -n "$host" ]] && BABA_CORS_ORIGINS="https://${host}"
            BABA_COOKIE_SECURE="true"
            c_ok "BABA_CORS_ORIGINS=$BABA_CORS_ORIGINS"
            c_ok "BABA_COOKIE_SECURE=true"
        else
            BABA_COOKIE_SECURE="false"
            c_dim "  Plain-HTTP localhost mode."
        fi
    fi

    # ── Stage 6: models ────────────────────────────────────────────────
    c_head "6/6  Default models"
    if [[ -n "$FLAG_MODELS" ]]; then
        MODELS_TIER="$FLAG_MODELS"
        c_dim "  Using --models=$MODELS_TIER"
    elif (( NONINTERACTIVE == 1 )); then
        MODELS_TIER="minimal"
        c_dim "  Non-interactive: defaulting to minimal."
    elif [[ -d "$BABA_MODELS_HOST" ]] && ls "$BABA_MODELS_HOST"/*.onnx >/dev/null 2>&1; then
        c_dim "  Models dir already has .onnx files — skipping download."
        MODELS_TIER="skip"
    else
        local pick
        pick=$(ask_choice "Download which set?" 2 \
            "Skip — bring your own ONNX" \
            "Minimal — RT-DETRv2-R18 + DINOv2-S + face stack (~430 MB)" \
            "Full zoo — every detector size + DINOv2-B + face (~3 GB)")
        case "$pick" in
            1) MODELS_TIER="skip";;
            2) MODELS_TIER="minimal";;
            3) MODELS_TIER="full";;
        esac
    fi

    # ── Fill remaining fixed fields ────────────────────────────────────
    # Detector model default: the GPU variants' decoders write NV12. nvidia
    # needs the nv12-baked graph (produced by the model download step) —
    # TensorRT has no PrePostProcessor, so it names the baked file even when a
    # skip-tier operator brings their own. On intel OpenVINO wraps a plain
    # model with an NV12 PrePostProcessor, so the bake is an optimisation.
    # CPU decode writes RGB → plain float32 model. An existing
    # .env value is preserved, as everywhere else in this wizard.
    BABA_DETECTOR_MODEL=""; BABA_DETECTOR_MODEL_FAMILY=""; BABA_DETECTOR_OV_PRECISION=""
    if (( VARIANT_CHANGED == 0 )); then
        BABA_DETECTOR_MODEL=$(envgen_read "$ENV_FILE" BABA_DETECTOR_MODEL || echo "")
        BABA_DETECTOR_MODEL_FAMILY=$(envgen_read "$ENV_FILE" BABA_DETECTOR_MODEL_FAMILY || echo "")
        BABA_DETECTOR_OV_PRECISION=$(envgen_read "$ENV_FILE" BABA_DETECTOR_OV_PRECISION || echo "")
    fi
    BABA_DETECTOR_DOWNLOAD=""   # plain detector basename the model step must fetch
    if [[ -z "$BABA_DETECTOR_MODEL" ]]; then
        # Pick the detector for this GPU tier (see detect_gpu_tier):
        #   dgpu-arc → RF-DETR-nano (cleaner night-IR FP profile, fp16-stable on
        #              OV; DINOv2 backbone runs well on Arc's dedicated VRAM)
        #   igpu     → D-FINE-s (lightweight CNN, compute-bound = iGPU-friendly)
        #   nvidia   → D-FINE-s (CNN; RF-DETR TensorRT head not wired yet)
        #   cpu      → RT-DETRv2-R18
        local tier detbase
        tier=$(detect_gpu_tier)
        case "$tier" in
            dgpu-arc) detbase="rf-detr-nano"; BABA_DETECTOR_MODEL_FAMILY="rfdetr"; BABA_DETECTOR_OV_PRECISION="f16" ;;
            igpu|dgpu-nvidia) detbase="d-fine-s"; BABA_DETECTOR_MODEL_FAMILY="dfine" ;;
            *) detbase="rtdetrv2-r18"; BABA_DETECTOR_MODEL_FAMILY="dfine" ;;
        esac
        c_ok "GPU tier: $tier → detector $detbase (family=$BABA_DETECTOR_MODEL_FAMILY)"
        BABA_DETECTOR_DOWNLOAD="$detbase"
        # RF-DETR needs its DINOv2 ImageNet norm + square stretch, which the OV
        # backend folds into the on-device PPP around the PLAIN model — the
        # BT.709 nv12 bake (right for D-FINE/RT-DETR) would be wrong. So rfdetr
        # rides the plain .onnx; the CNN families ride the nv12-baked variant on
        # GPU decode.
        if [[ "$BABA_DETECTOR_MODEL_FAMILY" == "rfdetr" ]]; then
            BABA_DETECTOR_MODEL="/models/${detbase}.onnx"
        elif [[ "$BABA_VARIANT" == "nvidia" || ( "$BABA_VARIANT" == "intel" && "$MODELS_TIER" != "skip" ) ]]; then
            BABA_DETECTOR_MODEL="/models/${detbase}.nv12.onnx"
        else
            BABA_DETECTOR_MODEL="/models/${detbase}.onnx"
        fi
    fi
    [[ -z "$BABA_DETECTOR_MODEL_FAMILY" ]] && BABA_DETECTOR_MODEL_FAMILY="dfine"
    # Intel render gid: grants /dev/dri access to the uid-1000 GPU services via
    # the intel compose override's group_add. Preserve an existing value; else
    # detect from the host on the intel variant; else a harmless default (unused
    # off-intel). Wrong/absent here = OpenVINO sees no GPU → require-GPU crash.
    BABA_INTEL_RENDER_GID=""
    (( VARIANT_CHANGED == 0 )) && BABA_INTEL_RENDER_GID=$(envgen_read "$ENV_FILE" BABA_INTEL_RENDER_GID || echo "")
    if [[ -z "$BABA_INTEL_RENDER_GID" ]]; then
        if [[ "$BABA_VARIANT" == "intel" ]]; then
            BABA_INTEL_RENDER_GID=$(detect_render_gid)
            c_ok "Intel render group gid: $BABA_INTEL_RENDER_GID"
        else
            BABA_INTEL_RENDER_GID=110
        fi
    fi
    BABA_API_PORT=$(envgen_read "$ENV_FILE" BABA_API_PORT || echo "8080")
    POSTGRES_USER=$(envgen_read "$ENV_FILE" POSTGRES_USER || echo "baba")
    POSTGRES_DB=$(envgen_read "$ENV_FILE" POSTGRES_DB || echo "baba")
    POSTGRES_SSLMODE=$(envgen_read "$ENV_FILE" POSTGRES_SSLMODE || echo "")
    # Postgres password: generate if not already set. Unlike NATS, the
    # postgres password is needed at first-boot for the initdb step,
    # so a regenerate-on-reinstall would orphan the existing data
    # directory. Preserve at all costs.
    POSTGRES_PASSWORD=$(envgen_read "$ENV_FILE" POSTGRES_PASSWORD || echo "")
    if [[ -z "$POSTGRES_PASSWORD" ]]; then
        POSTGRES_PASSWORD=$(secrets_nats_password)
    fi
    # JWT signing key — generate exactly once.
    BABA_SECRET_KEY=$(envgen_read "$ENV_FILE" BABA_SECRET_KEY || echo "")
    if [[ -z "$BABA_SECRET_KEY" ]]; then
        BABA_SECRET_KEY=$(secrets_jwt_key)
        c_ok "Generated BABA_SECRET_KEY."
    else
        c_dim "  Keeping existing BABA_SECRET_KEY."
    fi
}

print_summary() {
    c_head "Summary"
    cat <<EOF
  Variant:      $BABA_VARIANT
  Models host:  $BABA_MODELS_HOST
  State host:   $BABA_STATE_HOST
  Media host:   $BABA_MEDIA_HOST
  Admin:        $BABA_ADMIN_USERNAME$( [[ -z "$BABA_ADMIN_PASSWORD" ]] && echo " (auto-generated password)" )
  NATS user:    $NATS_USER
  CORS:         ${BABA_CORS_ORIGINS:-<dev defaults>}
  Cookie:       Secure=$BABA_COOKIE_SECURE
  Models:       ${MODELS_TIER}
  API port:     $BABA_API_PORT
EOF
}

# --- execution ------------------------------------------------------------

execute_install() {
    if (( DRY_RUN == 1 )); then
        c_warn "Dry run — stopping here. Above is what would be written / built."
        exit 0
    fi

    BABA_UID="${BABA_UID:-1000}"
    BABA_GID="${BABA_GID:-1000}"
    export BABA_UID BABA_GID

    # Export every var the envgen template references, so envgen_write's
    # parameter expansion picks them up. Local-scope assignments in the
    # wizard functions don't propagate to subshells; export does.
    export BABA_MODELS_HOST BABA_STATE_HOST BABA_MEDIA_HOST
    export BABA_VARIANT BABA_DECODER_BACKEND BABA_DETECTOR_MODEL
    export BABA_DETECTOR_MODEL_FAMILY BABA_DETECTOR_OV_PRECISION
    export BABA_INTEL_RENDER_GID
    export POSTGRES_USER POSTGRES_PASSWORD POSTGRES_DB POSTGRES_SSLMODE
    export NATS_USER NATS_PASSWORD BABA_PEER_NATS_PASSWORD
    export BABA_GO2RTC_API_USER BABA_GO2RTC_API_PASSWORD
    export BABA_API_PORT BABA_ADMIN_USERNAME BABA_ADMIN_PASSWORD BABA_SECRET_KEY
    export BABA_COOKIE_SECURE BABA_CORS_ORIGINS

    # The base compose is hardware-neutral; the GPU deploy lives in a per-variant
    # override. Bake the right file set into COMPOSE_FILE so EVERY `docker
    # compose` (this installer AND manual commands on the host) merges it
    # transparently — no -f juggling, no accidental GPU-less `up`.
    case "$BABA_VARIANT" in
        nvidia) COMPOSE_FILE="docker-compose.yml:docker-compose.nvidia.yml" ;;
        intel)  COMPOSE_FILE="docker-compose.yml:docker-compose.intel.yml" ;;
        *)      COMPOSE_FILE="docker-compose.yml" ;;
    esac
    export COMPOSE_FILE

    sync_submodules
    BABA_IMAGE_TAG="$(release_version)"
    export BABA_IMAGE_TAG

    if (( EXISTING_INSTALL == 1 && RECONFIGURE_MODE == 1 )); then
        # Reconfigure: .env is updated in place, never regenerated — keys the
        # wizard does not ask about (tuning, hand edits) stay as they are, and
        # secrets were carried over from .env by the wizard itself.
        envgen_retire "$ENV_FILE"
        envgen_merge_missing "$ENV_FILE"
        envgen_apply "$ENV_FILE" \
            BABA_MODELS_HOST BABA_STATE_HOST BABA_MEDIA_HOST \
            BABA_VARIANT COMPOSE_FILE BABA_IMAGE_TAG BABA_DECODER_BACKEND BABA_INTEL_RENDER_GID \
            BABA_DETECTOR_MODEL BABA_DETECTOR_MODEL_FAMILY BABA_DETECTOR_OV_PRECISION \
            BABA_ADMIN_USERNAME BABA_COOKIE_SECURE BABA_CORS_ORIGINS \
            NATS_USER NATS_PASSWORD BABA_PEER_NATS_PASSWORD
    else
        envgen_write "$ENV_FILE"
    fi

    # Seed the runtime go2rtc config from the credential-free template on
    # first run. go2rtc.yaml is gitignored (it holds camera RTSP credentials
    # once go2rtc_sync populates streams into it) so a fresh clone won't have
    # it — and a missing bind-mount target would make docker create an empty
    # directory. Never overwrite an existing runtime file.
    if [[ ! -e "$SCRIPT_DIR/go2rtc.yaml" ]]; then
        cp "$SCRIPT_DIR/go2rtc.example.yaml" "$SCRIPT_DIR/go2rtc.yaml"
        c_dim "  Seeded go2rtc.yaml from template"
    fi

    # App revision stamp: compose bind-mounts ./revision.json read-only into
    # the api (GET /version). It's git-derived and gitignored, so a fresh
    # clone/tarball doesn't have it — and a missing bind source would make
    # dockerd create a root-owned DIRECTORY named revision.json. The script
    # degrades gracefully without git (version <base>.0, sha unknown).
    bash "$SCRIPT_DIR/scripts/gen-revision.sh"
    c_dim "  Stamped revision.json"

    # Get service images: prebuilt from the registry (fast path — no 30 GB
    # CUDA base pull, no ffmpeg compile) with graceful fallback to a local
    # from-source build. COMPOSE_FILE (set above, written into .env) selects
    # the base + hardware override for every compose command below.
    if [[ "$INSTALL_SOURCE" == "build" ]]; then
        compose_build_base "$BABA_VARIANT"
        compose_build_services
    else
        if compose_pull_images; then
            :
        elif [[ "$INSTALL_SOURCE" == "pull" ]]; then
            c_err "--pull requested but images could not be pulled (offline? registry down?)."
            exit 1
        else
            c_warn "Prebuilt images unavailable — building locally from source (slower, one-time)."
            compose_build_base "$BABA_VARIANT"
            compose_build_services
        fi
    fi

    if [[ "$MODELS_TIER" != "skip" ]]; then
        compose_download_models "$MODELS_TIER" "$BABA_VARIANT" "$BABA_DETECTOR_DOWNLOAD"
    fi
    compose_up
    compose_wait_api_healthy 180  # generous — postgres initdb + migrations + model loads

    # Optional cgroup governance for the baba.slice parent (all containers run
    # under it via the compose cgroup_parent). Applied only when set — see
    # configure_resource_limits.
    configure_resource_limits

    # Self-healing watchdog: restart any baba-* container that hard-hangs into
    # `unhealthy` (docker's restart policy only acts on EXIT, not unhealthy).
    configure_health_watchdog

    # Optional scheduled DR backup (opt-in via BABA_BACKUP_SCHEDULE).
    configure_backup_timer

}

# Optional cgroup resource governance for the baba.slice parent (every BABA
# container runs under it via the compose cgroup_parent). Opt-in: both knobs
# default empty, so a DEDICATED single-tenant host leaves BABA unconstrained
# (capping it there would be wrong). On a SHARED host — BABA co-tenanted with
# other stacks — set them in .env so BABA can't be starved for CPU (CPUWeight,
# a soft priority under contention) nor OOM the box (MemoryMax, a hard cap):
#   BABA_MEM_LIMIT=8G       # hard memory ceiling for ALL baba containers
#   BABA_CPU_WEIGHT=1000    # scheduler weight (default 100) — 10x = priority
# systemctl set-property applies live AND persists across reboots. Non-fatal:
# BABA runs fine without it; the limits are a shared-host nicety.
configure_resource_limits() {
    local mem cpu props=()
    mem=$(envgen_read "$ENV_FILE" BABA_MEM_LIMIT  2>/dev/null || echo "")
    cpu=$(envgen_read "$ENV_FILE" BABA_CPU_WEIGHT 2>/dev/null || echo "")
    [[ -n "$mem" ]] && props+=("MemoryMax=$mem")
    [[ -n "$cpu" ]] && props+=("CPUWeight=$cpu")
    (( ${#props[@]} == 0 )) && return 0   # unset → BABA unconstrained (dedicated host)

    c_step "Applying baba.slice resource limits (${props[*]})…"
    if ! command -v systemctl >/dev/null 2>&1; then
        c_warn "  systemctl not found — skipping (non-systemd host)."
        return 0
    fi
    # Needs root + the slice to already exist (compose up created it above).
    if as_root systemctl set-property baba.slice "${props[@]}" 2>/dev/null; then
        c_ok "  ${props[*]}"
    else
        c_warn "  Could not apply (needs sudo + docker's systemd cgroup driver). Set manually:"
        c_dim "    sudo systemctl set-property baba.slice ${props[*]}"
    fi
}

# Self-healing watchdog. Docker's `restart: unless-stopped` fires on process
# EXIT but NOT on an `unhealthy` healthcheck, so a hard-hung service (wedged
# event loop, stuck libav/NATS) stays up-but-unhealthy forever. This installs a
# tiny host-side systemd timer that restarts any baba-* container stuck
# `unhealthy` (see scripts/heal-unhealthy.sh). It's safe because the SHM readers
# now share a /dev/shm VOLUME (a restart re-attaches cleanly instead of failing
# on a stale IPC namespace), it keys off the beat-based health only (never
# input-starvation, so a camera/network outage can't trigger a restart storm),
# and it has a circuit breaker. Opt out with BABA_HEALTH_WATCHDOG=0 in .env.

# A root systemd unit that ExecStart's a script inside the operator-owned
# checkout lets any non-root user who can write that checkout run code AS ROOT
# on the next timer tick. So the executed copy must be root-owned and not
# writable by the operator: install it into a root-only directory and point the
# unit there. The checkout stays the source; this copy is refreshed on every
# install/upgrade, which is also the only path that changes the units.
BABA_ROOT_LIBDIR=/usr/local/lib/baba
install_root_script() {
    # $1 = path to the source script in the checkout. Echoes the installed path.
    local src="$1" dest="$BABA_ROOT_LIBDIR/$(basename "$1")"
    as_root install -d -m 0755 -o root -g root "$BABA_ROOT_LIBDIR"
    as_root install -m 0755 -o root -g root "$src" "$dest"
    printf '%s\n' "$dest"
}

configure_health_watchdog() {
    local enabled
    enabled=$(envgen_read "$ENV_FILE" BABA_HEALTH_WATCHDOG 2>/dev/null || echo "1")
    [[ "$enabled" == "0" || "$enabled" == "false" ]] && return 0
    command -v systemctl >/dev/null 2>&1 || { c_warn "  health watchdog: no systemd — skipping."; return 0; }

    c_step "Installing baba-healwatch self-healing timer…"
    local heal_bin
    heal_bin=$(install_root_script "$SCRIPT_DIR/scripts/heal-unhealthy.sh")
    if ! as_root tee /etc/systemd/system/baba-healwatch.service >/dev/null <<EOF
[Unit]
Description=BABA unhealthy-container watchdog (restarts hard-hung services)
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
ExecStart=$heal_bin
EOF
    then
        c_warn "  Could not write systemd unit (needs sudo) — skipping watchdog."
        return 0
    fi
    as_root tee /etc/systemd/system/baba-healwatch.timer >/dev/null <<'EOF'
[Unit]
Description=Run the BABA health watchdog every minute

[Timer]
OnBootSec=120s
OnUnitActiveSec=60s
AccuracySec=10s

[Install]
WantedBy=timers.target
EOF
    as_root systemctl daemon-reload 2>/dev/null
    if as_root systemctl enable --now baba-healwatch.timer 2>/dev/null; then
        c_ok "  baba-healwatch.timer active (restarts unhealthy baba-* containers)"
    else
        c_warn "  Could not enable timer — run: sudo systemctl enable --now baba-healwatch.timer"
    fi
}

# Optional scheduled DR backup (scripts/backup.sh → pg_dump + irreplaceable
# media). Opt-in: set BABA_BACKUP_SCHEDULE to a systemd OnCalendar value in .env
# (e.g. "daily", "*-*-* 03:00:00"). Empty = no timer (run backup.sh manually).
configure_backup_timer() {
    local sched
    sched=$(envgen_read "$ENV_FILE" BABA_BACKUP_SCHEDULE 2>/dev/null || echo "")
    [[ -z "$sched" ]] && return 0
    command -v systemctl >/dev/null 2>&1 || { c_warn "  backup timer: no systemd — skipping."; return 0; }

    c_step "Installing baba-backup timer (schedule: $sched)…"
    local backup_bin
    backup_bin=$(install_root_script "$SCRIPT_DIR/scripts/backup.sh")
    if ! as_root tee /etc/systemd/system/baba-backup.service >/dev/null <<EOF
[Unit]
Description=BABA disaster-recovery backup (pg_dump + irreplaceable media)
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
Environment=BABA_CHECKOUT_DIR=$SCRIPT_DIR
ExecStart=$backup_bin
EOF
    then
        c_warn "  Could not write systemd unit (needs sudo) — skipping backup timer."
        return 0
    fi
    as_root tee /etc/systemd/system/baba-backup.timer >/dev/null <<EOF
[Unit]
Description=Run the BABA DR backup on schedule

[Timer]
OnCalendar=$sched
Persistent=true
AccuracySec=1min

[Install]
WantedBy=timers.target
EOF
    as_root systemctl daemon-reload 2>/dev/null
    if as_root systemctl enable --now baba-backup.timer 2>/dev/null; then
        c_ok "  baba-backup.timer active ($sched)"
    else
        c_warn "  Could not enable timer — run: sudo systemctl enable --now baba-backup.timer"
    fi
}

print_success() {
    c_head "✓ BABA running"
    cat <<EOF

  Dashboard:        http://localhost:${BABA_API_PORT:-8080}/  (api)
                    http://localhost:5173/                    (web)
EOF
    if [[ -z "$BABA_ADMIN_PASSWORD" ]]; then
        cat <<EOF

  Bootstrap admin password (read once, then delete):
    ${BABA_STATE_HOST}/api/bootstrap_admin_password
EOF
    else
        c_dim "  Admin password was set during install."
    fi
    cat <<EOF

  Logs:             docker compose logs -f api
  Restart stack:    ./install.sh --upgrade
  Reconfigure:      ./install.sh --reconfigure
  Stop:             docker compose down

  First-run setup continues in the web onboarding wizard.
EOF
}

# The release this checkout is exactly (tag vX.Y.Z, printed without the v),
# or nothing.
release_version() {
    local tag
    tag=$(git -C "$SCRIPT_DIR" describe --tags --exact-match --match 'v[0-9]*' HEAD 2>/dev/null) || return 0
    printf '%s\n' "${tag#v}"
}

# A plain `git clone` leaves the kit and home-core submodules empty, and a local
# build needs both. A clean release checkout is brought to exactly that
# release's pins, which is also what an upgrade to a newer tag needs; any other
# checkout only gets what is missing, so a moved submodule is never reset.
sync_submodules() {
    git -C "$SCRIPT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1 || return 0
    local missing
    if [[ -n "$(release_version)" ]] \
        && [[ -z "$(git -C "$SCRIPT_DIR" status --porcelain --ignore-submodules=all)" ]] \
        && [[ -z "$(git -C "$SCRIPT_DIR" submodule foreach --quiet 'git status --porcelain')" ]]; then
        git -C "$SCRIPT_DIR" submodule update --init --quiet
    else
        missing=$(git -C "$SCRIPT_DIR" submodule status | awk '/^-/ {print $2}')
        # shellcheck disable=SC2086
        [[ -z "$missing" ]] || git -C "$SCRIPT_DIR" submodule update --init --quiet -- $missing
    fi
}

# The images that match this checkout: a release tag pins that release's
# <variant>-<version> images, anything else takes the moving <variant> tag or a
# local build. It follows the checkout, so every run derives it again rather
# than keeping it as a setting.
pin_images() {
    BABA_IMAGE_TAG="$(release_version)"
    export BABA_IMAGE_TAG
    [[ "$(envgen_read "$ENV_FILE" BABA_IMAGE_TAG || true)" == "$BABA_IMAGE_TAG" ]] \
        || envgen_apply "$ENV_FILE" BABA_IMAGE_TAG
}

# --- main flow ------------------------------------------------------------

if (( UPGRADE_MODE == 1 )); then
    c_head "Upgrade mode"
    c_info "Keeping all existing config. Will append any new env keys,"
    c_info "rebuild images, and restart the stack."
    # Even in upgrade mode we need the env vars in scope for compose
    # commands. Source .env into the current shell.
    set -o allexport
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +o allexport
    BABA_ADMIN_PASSWORD="${BABA_ADMIN_PASSWORD:-}"
    MODELS_TIER="${FLAG_MODELS:-skip}"   # never re-download on upgrade unless asked
    ensure_credentials
    if (( DRY_RUN == 1 )); then
        c_warn "Dry run — stopping before .env is touched or anything is built."
        exit 0
    fi
    (( ${#CREDENTIALS_GENERATED[@]} > 0 )) && envgen_apply "$ENV_FILE" "${CREDENTIALS_GENERATED[@]}"
    envgen_retire "$ENV_FILE"
    envgen_merge_missing "$ENV_FILE"
    sync_submodules
    pin_images
    # Refresh the version stamp (safe without git).
    bash "$SCRIPT_DIR/scripts/gen-revision.sh"
    # Upgrade keeps the historical build-from-source default (a checkout that
    # deploys from git wants its OWN source, not the last public release);
    # --pull opts in to registry images instead.
    if [[ "$INSTALL_SOURCE" == "pull" ]]; then
        compose_pull_images || { c_err "--pull requested but images could not be pulled."; exit 1; }
    else
        compose_build_base "$BABA_VARIANT"
        compose_build_services
    fi
    # Recompute COMPOSE_FILE from the (possibly just-merged) variant so an
    # upgrade whose old .env predates this key still merges the GPU override —
    # otherwise a plain `up` would drop the nvidia reservation.
    case "$BABA_VARIANT" in
        nvidia) COMPOSE_FILE="docker-compose.yml:docker-compose.nvidia.yml" ;;
        intel)  COMPOSE_FILE="docker-compose.yml:docker-compose.intel.yml" ;;
        *)      COMPOSE_FILE="docker-compose.yml" ;;
    esac
    export COMPOSE_FILE
    if [[ "$MODELS_TIER" != "skip" ]]; then
        compose_download_models "$MODELS_TIER" "$BABA_VARIANT"
    fi
    compose_up
    compose_wait_api_healthy 180
    configure_resource_limits
    configure_health_watchdog
    configure_backup_timer
    print_success
    exit 0
fi

run_wizard
print_summary
if (( NONINTERACTIVE == 0 )); then
    if ! ask_yes_no "Proceed?" yes; then
        c_info "Cancelled."
        exit 0
    fi
fi
execute_install
print_success
