# shellcheck shell=bash
# Color output helpers. No-op when stdout isn't a TTY (CI logs, piped
# install, redirected output) — `tput setaf` would still emit ANSI on
# a non-TTY which clutters log files.

if [[ -t 1 ]] && command -v tput >/dev/null 2>&1 && [[ "$(tput colors 2>/dev/null || echo 0)" -ge 8 ]]; then
    _C_BOLD=$(tput bold)
    _C_RESET=$(tput sgr0)
    _C_DIM=$(tput dim)
    _C_RED=$(tput setaf 1)
    _C_GREEN=$(tput setaf 2)
    _C_YELLOW=$(tput setaf 3)
    _C_BLUE=$(tput setaf 4)
    _C_CYAN=$(tput setaf 6)
else
    _C_BOLD=""; _C_RESET=""; _C_DIM=""
    _C_RED=""; _C_GREEN=""; _C_YELLOW=""; _C_BLUE=""; _C_CYAN=""
fi

# Single-line semantic helpers — each prints to the right stream so a
# downstream `2>install.log` separates errors from info.
c_info()  { printf '%s\n' "$*"; }
c_dim()   { printf '%s%s%s\n' "$_C_DIM" "$*" "$_C_RESET"; }
c_ok()    { printf '%s✓%s %s\n' "$_C_GREEN" "$_C_RESET" "$*"; }
c_warn()  { printf '%s⚠ %s%s\n' "$_C_YELLOW" "$*" "$_C_RESET" >&2; }
c_err()   { printf '%s✗ %s%s\n' "$_C_RED" "$*" "$_C_RESET" >&2; }
c_step()  { printf '%s▶%s %s\n' "$_C_BLUE" "$_C_RESET" "$*"; }
c_head()  {
    local title="$*"
    printf '\n%s═══ %s ═══%s\n' "$_C_BOLD" "$title" "$_C_RESET"
}
