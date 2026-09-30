#!/usr/bin/env bash
# Deploy BABA to one instance, or to all of them.
#
#   ./deploy/deploy.sh cabin            # one instance
#   ./deploy/deploy.sh prod nvidia     # several
#   ./deploy/deploy.sh all             # every instance in the inventory
#   ./deploy/deploy.sh --list          # show what's registered
#   ./deploy/deploy.sh --status        # show what each instance is RUNNING
#   ./deploy/deploy.sh --pull cabin     # take published images, don't build
#   ./deploy/deploy.sh --allow-unpushed …  # deploy a tree that isn't origin/main yet
#   ./deploy/deploy.sh --dry-run all   # print the plan, change nothing
#
# The instance list lives in deploy/hosts.conf — adding a box is a line there,
# never another copy of this script. Everything a deploy must get right (build
# the variant's base image before the services, keep the storage tiers owned by
# uid 1000, gate on health rather than on `up` returning, prune afterwards) is
# written once here, so no instance can quietly drift from the others.
#
# Source reaches a host either by its own `git pull` or by rsync from THIS
# checkout (see the `sync` column). Rsync never carries .env, models, media,
# state or logs: config and data belong to the instance, code belongs here.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
INVENTORY="$SCRIPT_DIR/hosts.conf"

DRY_RUN=0
PULL=0
ALLOW_UNPUSHED=0
DEPLOY_ALL=0
TARGETS=()

die() { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }
say() { printf '\033[36m▶ %s\033[0m\n' "$*"; }
ok()  { printf '\033[32m✓ %s\033[0m\n' "$*"; }

[[ -f "$INVENTORY" ]] || die "inventory not found: $INVENTORY (start from hosts.conf.example)"

# --- inventory ------------------------------------------------------------

inventory_rows() { grep -vE '^\s*(#|$)' "$INVENTORY"; }

list_hosts() {
    printf '%-10s %-24s %-8s %-18s %s\n' NAME SSH VARIANT PATH FLAGS
    inventory_rows | while read -r name ssh variant path flags; do
        printf '%-10s %-24s %-8s %-18s %s\n' "$name" "$ssh" "$variant" "$path" "$flags"
    done
}

# What each instance is actually RUNNING, next to what this checkout would ship.
# The stamp is revision.json, written by the deploy. A host's own .git is frozen
# wherever its last clone left it — source arrives by rsync — so `git log` there
# reports a commit that has nothing to do with the code in its containers, which
# is a misleading enough signal to send someone chasing drift that isn't there.
status_hosts() {
    local here rev
    here=$(git -C "$REPO_ROOT" rev-parse --short=8 HEAD 2>/dev/null || echo '?')
    printf '%-10s %-12s %s\n' NAME RUNNING STATE
    while read -r name ssh variant path flags; do
        # -n: without it ssh drains the loop's stdin and only the first host is
        # ever reported — which would make this command lie by omission.
        # `|| true`: under `set -e` a failed command substitution in an assignment
        # ends the script, so one unreachable box used to truncate the table after
        # it — the exact lie by omission the -n above is there to prevent.
        rev=$(ssh -n -o BatchMode=yes -o ConnectTimeout=10 "$ssh" \
              "sed -n 's/.*\"sha\": *\"\([^\"]*\)\".*/\1/p' '$path/revision.json' 2>/dev/null" 2>/dev/null || true)
        [[ -n "$rev" ]] || rev='unreachable'
        printf '%-10s %-12s %s\n' "$name" "$rev" \
            "$(if [[ "$rev" == unreachable ]]; then echo 'could not be asked'
               elif [[ "$rev" == "$here" ]]; then echo 'up to date'
               else echo "differs — this checkout is $here"; fi)"
    done < <(inventory_rows)
}

row_for() {
    local want="$1" name ssh variant path flags
    while read -r name ssh variant path flags; do
        [[ "$name" == "$want" ]] && { printf '%s\t%s\t%s\t%s\n' "$ssh" "$variant" "$path" "$flags"; return 0; }
    done < <(inventory_rows)
    return 1
}

# --- args -----------------------------------------------------------------

while [[ $# -gt 0 ]]; do
    case "$1" in
        --list) list_hosts; exit 0;;
        --status) status_hosts; exit 0;;
        --dry-run) DRY_RUN=1; shift;;
        --pull) PULL=1; shift;;
        --allow-unpushed) ALLOW_UNPUSHED=1; shift;;
        -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
        -*) die "unknown flag: $1";;
        all) DEPLOY_ALL=1
             while read -r n _; do TARGETS+=("$n"); done < <(inventory_rows); shift;;
        *) TARGETS+=("$1"); shift;;
    esac
done

(( ${#TARGETS[@]} )) || die "nothing to deploy — pass instance name(s), or 'all' (see --list)"

# --- one deploy at a time -------------------------------------------------
# Two deploys interleaving on one host race docker compose against itself and
# die on "container name already in use" — observed three times in two days,
# every time a second deploy was launched while the first still ran. Waiting
# is always correct here: the second deploy carries the newer commit and
# should simply go after the first finishes. Lock is per-checkout (flock on a
# repo-local file), released automatically when the process exits.
# Per INSTANCE, not per checkout. The race being prevented is two deploys
# interleaving on ONE host, and a single lock over the whole run made a slow
# box block a fast one: cabin builds over a 0.79 Mbit/s uplink, and a deploy
# waiting on it left production sitting behind a reference instance for twenty
# minutes with a fix already pushed.
lock_instance() {
    local name="$1" fd
    exec {fd}>"$REPO_ROOT/.deploy.lock.$name"
    if ! flock -n "$fd"; then
        say "$name: another deploy is on it — waiting"
        flock "$fd"
    fi
    LOCK_FD="$fd"
    # `exec {fd}>` opens at PROCESS scope, so the fd (and its flock) outlives
    # deploy_one and is inherited by the background half spawned below — which
    # then holds a foreground instance's lock for its whole run. That is how a
    # prod deploy sat blocked behind the nvidia/cabin background build. Record
    # every lock fd so the background spawn can close them first.
    LOCK_FDS+=("$fd")
}
LOCK_FDS=()

# --- the deploying checkout must BE origin/main --------------------------
# Bytes travel from here, but the authority is the pushed branch. Without this
# an instance could end up running a commit that exists on no one else's
# machine — unreviewable, unreproducible, and impossible to roll back to.
# --allow-unpushed is the deliberate escape hatch for a hotfix under fire.
require_pushed_head() {
    (( ALLOW_UNPUSHED )) && { say "skipping the origin/main check (--allow-unpushed)"; return 0; }
    git -C "$REPO_ROOT" fetch --quiet origin main 2>/dev/null \
        || die "cannot reach origin — deploying an unverified tree needs --allow-unpushed"
    local head origin dirty
    head="$(git -C "$REPO_ROOT" rev-parse HEAD)"
    origin="$(git -C "$REPO_ROOT" rev-parse origin/main)"
    [[ "$head" == "$origin" ]] \
        || die "HEAD ($(git -C "$REPO_ROOT" rev-parse --short HEAD)) is not origin/main ($(git -C "$REPO_ROOT" rev-parse --short origin/main)) — push (or pull) first, or use --allow-unpushed"
    # Uncommitted work is invisible to `git archive`, so a deploy would silently
    # omit it. Say so rather than let someone wonder why their edit did nothing.
    dirty="$(git -C "$REPO_ROOT" status --porcelain | head -5)"
    if [[ -n "$dirty" ]]; then
        c_warn_lines() { printf '\033[33m⚠ %s\033[0m\n' "$*" >&2; }
        c_warn_lines "uncommitted changes are NOT deployed (git archive ships HEAD):"
        printf '%s\n' "$dirty" | sed 's/^/    /' >&2
    fi
    ok "deploying origin/main @ $(git -C "$REPO_ROOT" rev-parse --short HEAD)"
}
require_pushed_head

# --- can the previous images be put back? ---------------------------------
# Decided HERE, not on the instance: the instance has no .git (source arrives as
# a tar), and this is the only machine that can read what changed between the
# revision it is running and the one being shipped. Anything that drops schema
# makes a rollback worse than the failure it would answer, and a revision we
# cannot resolve is not an argument that the swap is safe. So does retiring an
# .env key: remote.sh removes it before the build, and the previous images,
# which still read it, would come back up misconfigured.
retired_env_keys() {
    sed -n '/^ENVGEN_RETIRED_KEYS=(/,/^)/{/^ENVGEN_RETIRED_KEYS=(/d;/^)/d;p;}' \
        | tr -s ' \t' '\n' | sed '/^$/d' | sort -u
}

rollback_verdict() {
    local ssh="$1" path="$2" host_rev destructive="" retired m
    host_rev=$(ssh -n -o BatchMode=yes -o ConnectTimeout=15 "$ssh" \
        "sed -n 's/.*\"sha\": *\"\([^\"]*\)\".*/\1/p' '$path/revision.json' 2>/dev/null" 2>/dev/null || true)
    if [[ -z "$host_rev" ]] || ! git -C "$REPO_ROOT" cat-file -e "${host_rev}^{commit}" 2>/dev/null; then
        printf '%s\t%s\n' "" "the revision this instance is running could not be read, so whether the schema moved is unknown"
        return 0
    fi
    while read -r m; do
        [[ -n "$m" ]] || continue
        git -C "$REPO_ROOT" show "HEAD:$m" 2>/dev/null \
            | grep -qiE '\bDROP[[:space:]]+(COLUMN|TABLE|TYPE)\b|\bDELETE[[:space:]]+FROM\b|\bTRUNCATE\b' \
            && destructive+="$m "
    done < <(git -C "$REPO_ROOT" diff --name-only "$host_rev" HEAD -- db/migrations/ 2>/dev/null || true)
    retired=$(comm -13 \
        <(git -C "$REPO_ROOT" show "$host_rev:scripts/lib/envgen.sh" 2>/dev/null | retired_env_keys) \
        <(git -C "$REPO_ROOT" show "HEAD:scripts/lib/envgen.sh" | retired_env_keys) | tr '\n' ' ')
    if [[ -n "$destructive" ]]; then
        printf '%s\t%s\n' "$destructive" "this deploy drops schema the previous images still read"
    elif [[ -n "$retired" ]]; then
        printf '%s\t%s\n' "" "this deploy retires .env keys the previous images still read: ${retired% }"
    else
        printf '%s\t%s\n' "" ""
    fi
}

# --- one instance ---------------------------------------------------------

deploy_one() {
    local name="$1" row ssh variant path flags LOCK_FD=
    row="$(row_for "$name")" || die "unknown instance '$name' (see --list)"
    IFS=$'\t' read -r ssh variant path flags <<<"$row"
    [[ "$flags" == "-" ]] && flags=""   # inventory placeholder for "none"

    say "$name — $ssh ($variant${flags:+, flags=$flags})"
    if (( DRY_RUN )); then
        if (( PULL )); then
            echo "   would: ship committed tree → back up + verify the database → snapshot images → pull published images → up → health-gate → roll back on failure → prune"
        else
            echo "   would: ship committed tree → back up + verify the database → snapshot images → build base-$variant + services → up → health-gate → roll back on failure → prune"
        fi
        return 0
    fi
    lock_instance "$name"

    ssh -o BatchMode=yes -o ConnectTimeout=15 "$ssh" "test -d '$path'" \
        || { printf '\033[31m✗ %s: cannot reach %s, or %s does not exist there\033[0m\n' "$name" "$ssh" "$path" >&2; return 1; }

    local destructive no_rollback verdict
    verdict="$(rollback_verdict "$ssh" "$path")"
    IFS=$'\t' read -r destructive no_rollback <<<"$verdict"
    [[ -n "$no_rollback" ]] && say "$name — image rollback disabled: $no_rollback"

    {
        say "$name — shipping committed tree ($(git -C "$REPO_ROOT" rev-parse --short HEAD))"
        # `git archive` rather than rsync: no dependency to install on either
        # end, and it ships exactly what is COMMITTED — a deploy that quietly
        # carried an editor's half-finished buffer would be worse than one that
        # refuses. Everything the instance owns (.env, models, media, state,
        # logs, runtime go2rtc.yaml) is gitignored, so it is excluded by
        # construction rather than by a list that can fall out of date.
        # Caveat: extraction overwrites, it does not delete. A file removed
        # from the repo lingers on an archive-synced host until it is cleaned
        # by hand; nothing imports it, so it is inert.
        # This is also how deploy/remote.sh — the half that runs over there —
        # gets there, so the instance always runs the version of it that belongs
        # to the commit being deployed.
        # The kit is a submodule, which `git archive` leaves empty; export-tree.sh
        # appends it at the commit HEAD records.
        bash "$REPO_ROOT/scripts/export-tree.sh" HEAD \
            | ssh -o BatchMode=yes -o ConnectTimeout=15 "$ssh" "tar -x -C '$path'" \
            || { printf '\033[31m✗ %s: source sync failed\033[0m\n' "$name" >&2; return 1; }

        # revision.json is git-derived and gitignored, so the archive cannot
        # carry it and the host has no .git to regenerate it from — the About
        # page would read "unknown · unknown · +local" and the instance could
        # not tell you which commit it runs. That is precisely the fact you
        # need to confirm a deploy landed, so ship it from here, where git is.
        # `dirty` is forced false: gen-revision.sh reports on the DEPLOYING
        # machine's working tree, but `git archive HEAD` can only ship committed
        # content — so the instance is byte-for-byte HEAD no matter what is
        # uncommitted here. Left as-is it stamped "+local" on provably clean
        # deploys, which also destroys the flag's only use: telling a modified
        # build apart from a reproducible one.
        # Staged, not stamped in place: the api bind-mounts revision.json and
        # reads it per request, so writing it here — before the build, the
        # recreate and the health gate — makes the About page claim a revision
        # the containers are not running yet, and keep claiming it forever if
        # the deploy then fails. The remote half moves it into place only once
        # everything is healthy.
        bash "$REPO_ROOT/scripts/gen-revision.sh" \
            && sed 's/"dirty": true/"dirty": false/' "$REPO_ROOT/revision.json" \
               | ssh -o BatchMode=yes -o ConnectTimeout=15 "$ssh" "cat > '$path/revision.json.pending'" \
            || { printf '\033[31m✗ %s: could not stamp revision.json\033[0m\n' "$name" >&2; return 1; }
    }

    # --- run the remote half DETACHED, then watch it ----------------------
    # Not `ssh … bash -s`. cabin is reached by ProxyJump through a host that falls
    # back to `cloudflared access ssh`, and a deploy restarts the very tunnel it
    # is arriving through: the connection dies mid-recreate, SIGHUP kills docker
    # compose, and the stack is left half-up with no way back in. That is how
    # DIDA lost the same box on 2026-08-07. Under setsid the deploy finishes —
    # health gate and rollback included — whatever happens to this ssh session,
    # and losing the watcher below costs only the watching.
    local log="/tmp/baba-deploy-$name.log"
    local launcher="/tmp/baba-launch-$name.sh"
    # The marker line is how the watcher below knows the run ended, and with what.
    # It is echoed by this wrapper rather than by remote.sh, so it is written even
    # when remote.sh dies on `set -e` half way through.
    if ! ssh -o BatchMode=yes -o ConnectTimeout=15 "$ssh" "rm -f '$log'; cat > '$launcher'" <<EOF
#!/bin/sh
export VARIANT='$variant' TARGET_PATH='$path' FLAGS='$flags' PULL='$PULL'
export DESTRUCTIVE='$destructive' NO_ROLLBACK='$no_rollback'
bash '$path/deploy/remote.sh'
echo "BABA_DEPLOY_DONE_\$?"
EOF
    then
        printf '\033[31m✗ %s: could not stage the remote half\033[0m\n' "$name" >&2
        return 1
    fi

    ssh -o BatchMode=yes -o ConnectTimeout=15 "$ssh" \
        "setsid sh -c \"sh '$launcher' > '$log' 2>&1\" >/dev/null 2>&1 &" \
        || { printf '\033[31m✗ %s: could not start the remote half\033[0m\n' "$name" >&2; return 1; }

    say "$name — running detached; watching $ssh:$log"
    # Up to ~90 min: a full rebuild over cabin's 0.79 Mbit/s uplink genuinely
    # takes that long, and giving up here loses only the WATCHER — which would
    # otherwise read as a failed deploy that is in fact still running.
    local rc="" line last=""
    for _ in $(seq 1 540); do
        sleep 10
        line=$(ssh -n -o BatchMode=yes -o ConnectTimeout=10 "$ssh" \
               "tail -n1 '$log' 2>/dev/null" 2>/dev/null || true)
        case "$line" in BABA_DEPLOY_DONE_*) rc="${line#BABA_DEPLOY_DONE_}"; break;; esac
        if [[ -n "$line" && "$line" != "$last" ]]; then
            printf '   %s\n' "$line"
            last="$line"
        fi
    done
    if [[ -z "$rc" ]]; then
        printf '\033[33m⚠ %s: stopped watching after 90 min — the remote half is detached and may still be running (%s: tail -f %s)\033[0m\n' \
            "$name" "$ssh" "$log" >&2
        return 1
    fi
    if [[ "$rc" != "0" ]]; then
        ssh -n -o BatchMode=yes -o ConnectTimeout=15 "$ssh" "tail -25 '$log'" 2>/dev/null || true
        printf '\033[31m✗ %s: remote deploy failed (rc=%s)\033[0m\n' "$name" "$rc" >&2
        return 1
    fi
    ok "$name deployed"
}

# `all` puts the first instance in the inventory in front of you and lets the
# rest carry on behind. The inventory is ordered by what it costs to have
# stale: production first, the boxes that exist to prove the variant still
# builds after it. Naming instances explicitly keeps every one of them in the
# foreground — if you asked for that box by name, you want to watch it.
foreground=("${TARGETS[@]}")
background=()
if (( DEPLOY_ALL && ${#TARGETS[@]} > 1 )); then
    foreground=("${TARGETS[0]}")
    background=("${TARGETS[@]:1}")
fi

failed=()
for t in "${foreground[@]}"; do
    deploy_one "$t" || failed+=("$t")
done

if (( ${#background[@]} && ! DRY_RUN )); then
    # Every flag that changed what the foreground did has to travel with it, or
    # the background half quietly deploys under different rules than the half
    # you watched.
    args=()
    (( PULL )) && args+=(--pull)
    (( ALLOW_UNPUSHED )) && args+=(--allow-unpushed)
    log="$REPO_ROOT/.deploy.background.log"
    # Drop the foreground instances' lock fds before forking: the foreground is
    # done with them, and an inherited copy would keep a lock held for the whole
    # background run (see lock_instance). The background re-locks its own
    # instances fresh.
    for _lfd in "${LOCK_FDS[@]}"; do eval "exec ${_lfd}>&-"; done
    setsid nohup "$0" "${args[@]}" "${background[@]}" \
        >"$log" 2>&1 < /dev/null &
    say "${background[*]} continue in the background — $log"
elif (( ${#background[@]} )); then
    echo "   would: then deploy ${background[*]} in the background"
fi

if (( ${#failed[@]} )); then
    die "failed: ${failed[*]}"
fi
ok "done: ${foreground[*]}"
