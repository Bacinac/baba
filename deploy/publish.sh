#!/usr/bin/env bash
# Build BABA's service images and publish them to the registry, so a fresh
# install on someone else's machine pulls working images instead of spending
# half an hour compiling ffmpeg on whatever hardware they have.
#
#   ./deploy/publish.sh intel          # one variant
#   ./deploy/publish.sh intel cpu      # several
#   ./deploy/publish.sh --dry-run all  # show the plan, build and push nothing
#   ./deploy/publish.sh --dirty-ok cpu # allow uncommitted changes in the tree
#   ./deploy/publish.sh --no-tag cpu   # skip the git tag
#
# Every image is pushed twice: <svc>:<variant>-<version> (pinned, what
# BABA_IMAGE_TAG=<version> selects) and <svc>:<variant> (moving alias, what a
# plain `install.sh --pull` takes). The version is git-derived by
# scripts/gen-revision.sh, and the commit is tagged v<version> so a pinned
# image can always be traced back to its source.
#
# Runs the build LOCALLY, on the machine you invoke it from. That is
# deliberate: building every variant in one place would mean pulling the ~10 GB
# CUDA base onto a box that has no NVIDIA GPU, and our dev host also carries
# other projects that a multi-hour build would starve. Build `intel`/`cpu`
# wherever is convenient and `nvidia` on the NVIDIA box, which already has that
# base cached. Nothing about the images is host-specific — they are plain
# amd64 — so where they were built does not matter to the puller.
#
# Auth: needs a classic PAT with `write:packages` (fine-grained PATs don't
# cover GHCR). Taken from $GITHUB_TOKEN, the file named by $GITHUB_TOKEN_FILE,
# or ./.gh-token (gitignored). Never echoed, never passed on a command line
# (it would land in the process table).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

source "$SCRIPT_DIR/images.sh"
REGISTRY="${IMAGE_BASE%%/*}"
REGISTRY_USER="${BABA_REGISTRY_USER:-bacinac}"

DRY_RUN=0
DIRTY_OK=0
DO_TAG=1
VARIANTS=()

die() { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }
say() { printf '\033[36m▶ %s\033[0m\n' "$*"; }
ok()  { printf '\033[32m✓ %s\033[0m\n' "$*"; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)  DRY_RUN=1; shift;;
        --dirty-ok) DIRTY_OK=1; shift;;
        --no-tag)   DO_TAG=0; shift;;
        -h|--help) sed -n '2,29p' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
        all) VARIANTS=("${ALL_VARIANTS[@]}"); shift;;
        intel|nvidia|cpu) VARIANTS+=("$1"); shift;;
        *) die "unknown argument: $1 (variants: ${ALL_VARIANTS[*]}, or 'all')";;
    esac
done
(( ${#VARIANTS[@]} )) || die "nothing to publish — pass a variant (${ALL_VARIANTS[*]}) or 'all'"

cd "$REPO_ROOT"

# The version is the commit count; outside a checkout (an rsync-shipped
# instance tree) gen-revision.sh would fall back to 0 and mislabel the images.
git rev-parse --is-inside-work-tree >/dev/null 2>&1 \
    || die "$REPO_ROOT is not a git checkout — publish from a clone, not a shipped instance tree"

# A published image must be reproducible from a commit. Parallel sessions often
# leave the tree dirty — force acknowledgement instead of silently baking
# uncommitted work into public images.
if (( ! DIRTY_OK )) && ! git diff --quiet --ignore-submodules HEAD; then
    die "working tree is dirty — commit first, or pass --dirty-ok"
fi

bash scripts/gen-revision.sh
VERSION="$(sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' revision.json)"
[[ -n "$VERSION" ]] || die "could not read the version from revision.json"

registry_login() {
    local token=""
    if [[ -n "${GITHUB_TOKEN:-}" ]]; then
        token="$GITHUB_TOKEN"
    elif [[ -n "${GITHUB_TOKEN_FILE:-}" && -r "$GITHUB_TOKEN_FILE" ]]; then
        token="$(tr -d ' \t\n\r' < "$GITHUB_TOKEN_FILE")"
    elif [[ -r .gh-token ]]; then
        token="$(tr -d ' \t\n\r' < .gh-token)"
    else
        die "no registry token — export GITHUB_TOKEN, point GITHUB_TOKEN_FILE at a file holding one, or create ./.gh-token (needs write:packages)"
    fi
    say "logging in to $REGISTRY as $REGISTRY_USER"
    printf '%s' "$token" | docker login "$REGISTRY" -u "$REGISTRY_USER" --password-stdin >/dev/null \
        || die "registry login failed"
}

# Build against the hardware-neutral compose file only; the GPU overrides add
# runtime device reservations, nothing build-relevant.
export COMPOSE_FILE=docker-compose.yml
# Building an image needs none of the runtime secrets, but compose refuses to
# interpolate without the variables it marks required (POSTGRES_PASSWORD). A
# publishing host is a CHECKOUT, not an instance — it
# has no .env and should not be given one, or someone will eventually deploy
# from it by accident. Supply throwaway values for the build only; they never
# reach an image (they are runtime env, not build args).
export POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-build-only-unused}"
# Compose validates volume specs even for `build`, so every storage path needs
# a value or it fails with "invalid spec: :/media: empty section between colons".
for _v in BABA_MODELS_HOST BABA_STATE_HOST BABA_MEDIA_HOST BABA_LOGS_HOST; do
    [[ -n "${!_v:-}" ]] || export "$_v=/tmp/baba-build-only/${_v,,}"
done
unset _v

# Compose builds and pushes the pinned name; the moving alias is a second tag
# on the same image.
push_pinned_and_alias() {
    local repo="$1" alias="$2"
    docker tag "$IMAGE_BASE/$repo:$alias-$VERSION" "$IMAGE_BASE/$repo:$alias"
    docker push -q "$IMAGE_BASE/$repo:$alias-$VERSION" >/dev/null
    docker push -q "$IMAGE_BASE/$repo:$alias" >/dev/null
    # The registry keeps the pinned name. Kept locally too, it would pin this
    # generation on the build box after every later deploy.
    docker rmi "$IMAGE_BASE/$repo:$alias-$VERSION" >/dev/null
}

publish_variant() {
    local variant="$1" svc
    say "$variant — building base + services (v$VERSION)"
    if (( DRY_RUN )); then
        echo "   would: build base-$variant → build ${#SERVICES[@]} services → push ${SERVICES[*]} as $IMAGE_BASE/<svc>:$variant-$VERSION + :$variant"
        return 0
    fi

    # Same ordering trap as a deploy: the base sits behind the `bases` profile,
    # so a plain `compose build` would publish services riding a stale base.
    BABA_VARIANT="$variant" docker compose --profile bases build --pull "base-$variant"
    BABA_VARIANT="$variant" BABA_IMAGE_TAG="$VERSION" docker compose build "${SERVICES[@]}"
    for svc in "${SERVICES[@]}"; do
        push_pinned_and_alias "$svc" "$variant"
    done
    ok "$variant pushed to $IMAGE_BASE/<service>:$variant-$VERSION + :$variant"
}

(( DRY_RUN )) || registry_login

for v in "${VARIANTS[@]}"; do
    publish_variant "$v"
done

# web (served, not inferred with) and nats (the bus with its config) are the
# same image for every variant, so each is built and pushed once rather than per
# variant. A later pass for another variant (nvidia on its own box) finds it
# already there; re-pushing would move a pinned tag to a different digest.
publish_common() {
    local repo="$1" alias="$2"
    if (( DRY_RUN )); then
        echo "   would: build + push $repo as $IMAGE_BASE/$repo:$alias-$VERSION + :$alias (unless already published)"
    elif docker manifest inspect "$IMAGE_BASE/$repo:$alias-$VERSION" >/dev/null 2>&1; then
        ok "$repo v$VERSION already published"
    else
        say "$repo — building + pushing (variant-independent)"
        BABA_IMAGE_TAG="$VERSION" docker compose build "$repo"
        push_pinned_and_alias "$repo" "$alias"
        ok "$repo pushed"
    fi
}
for image in "${COMMON_IMAGES[@]}"; do
    read -r repo alias <<< "$image"
    publish_common "$repo" "$alias"
done

# The tag is shared by every variant of this commit, so a second run (e.g. the
# nvidia pass on the NVIDIA box) finds it already there.
if (( DO_TAG )); then
    if (( DRY_RUN )); then
        echo "   would: git tag v$VERSION + push it to origin"
    elif git rev-parse -q --verify "refs/tags/v$VERSION" >/dev/null; then
        ok "git tag v$VERSION already exists"
    else
        git tag -a "v$VERSION" -m "BABA v$VERSION"
        git push -q origin "v$VERSION"
        ok "tagged v$VERSION"
    fi
fi

ok "published v$VERSION: ${VARIANTS[*]}"
echo
echo "A fresh install now pulls these:  ./install.sh --pull"
echo "Existing instances take them via: ./deploy/deploy.sh <instance>"
