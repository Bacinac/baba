# shellcheck shell=bash
# Interactive prompt helpers. All respect $NONINTERACTIVE — when set,
# they use the supplied default and never block on read. If a prompt
# has no default in non-interactive mode, the caller is expected to
# have provided the value via a flag; the helpers fail loud otherwise.
#
# Functions write to stderr so the prompts stay visible even when the
# caller captures stdout (e.g. `value=$(ask_text 'foo' 'bar')`).

# Print a prompt + default, read response, fall back to default on empty.
# Usage: value=$(ask_text "Prompt text" "default value")
ask_text() {
    local prompt="$1" default="${2:-}" reply
    if [[ "${NONINTERACTIVE:-0}" == "1" ]]; then
        if [[ -z "$default" ]]; then
            c_err "Non-interactive mode and no default for: $prompt"
            return 1
        fi
        printf '%s' "$default"
        return 0
    fi
    if [[ -n "$default" ]]; then
        printf '%s [%s]: ' "$prompt" "$default" >&2
    else
        printf '%s: ' "$prompt" >&2
    fi
    IFS= read -r reply || reply=""
    [[ -z "$reply" ]] && reply="$default"
    printf '%s' "$reply"
}

# Hidden-input variant. Used for typed passwords.
ask_password() {
    local prompt="$1" reply
    if [[ "${NONINTERACTIVE:-0}" == "1" ]]; then
        c_err "Cannot prompt for password in non-interactive mode"
        return 1
    fi
    printf '%s: ' "$prompt" >&2
    IFS= read -rs reply || reply=""
    printf '\n' >&2
    printf '%s' "$reply"
}

# Yes/no prompt. Returns 0 for yes, 1 for no. Default shown via case
# of the [Y/n] vs [y/N] hint.
# Usage: if ask_yes_no "Continue?" yes; then ...
ask_yes_no() {
    local prompt="$1" default="${2:-yes}" reply hint
    case "$default" in
        yes|y|true|1) hint="[Y/n]";;
        *) hint="[y/N]"; default="no";;
    esac
    if [[ "${NONINTERACTIVE:-0}" == "1" ]]; then
        [[ "$default" == "yes" ]] && return 0 || return 1
    fi
    printf '%s %s: ' "$prompt" "$hint" >&2
    IFS= read -r reply || reply=""
    [[ -z "$reply" ]] && reply="$default"
    case "$reply" in
        y|Y|yes|YES|true|1) return 0;;
        *) return 1;;
    esac
}

# Numbered-choice prompt. Args: prompt, default_index (1-based), then
# the option strings as positional args. Returns the chosen index
# (1-based) on stdout.
#
# Usage:
#   choice=$(ask_choice "Variant" 1 "nvidia (TensorRT)" "intel (OpenVINO)" "cpu (slow)")
#   case "$choice" in 1) ...;; 2) ...;; 3) ...;; esac
ask_choice() {
    local prompt="$1" default="$2"; shift 2
    local opts=("$@")
    local n=${#opts[@]}
    if (( default < 1 || default > n )); then default=1; fi
    if [[ "${NONINTERACTIVE:-0}" == "1" ]]; then
        printf '%s' "$default"
        return 0
    fi
    printf '%s:\n' "$prompt" >&2
    local i=1
    for opt in "${opts[@]}"; do
        if (( i == default )); then
            printf '  %d) %s   (default)\n' "$i" "$opt" >&2
        else
            printf '  %d) %s\n' "$i" "$opt" >&2
        fi
        i=$((i+1))
    done
    local reply
    while true; do
        printf 'Pick [%d]: ' "$default" >&2
        IFS= read -r reply || reply=""
        [[ -z "$reply" ]] && reply="$default"
        if [[ "$reply" =~ ^[0-9]+$ ]] && (( reply >= 1 && reply <= n )); then
            printf '%s' "$reply"
            return 0
        fi
        c_warn "Pick a number between 1 and $n"
    done
}
