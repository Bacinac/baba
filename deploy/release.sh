#!/usr/bin/env bash
# Publish a BABA release: the GitHub Release that boskovic.biz reads and pins its
# install command to. It comes last, after deploy/publish.sh has pushed this
# commit's images for every variant (intel and cpu here, nvidia on the NVIDIA
# box) and tagged the commit, and it is refused until a stranger without an
# account can pull every one of those images and clone everything the tag names.
#
#   ./deploy/release.sh            publish
#   ./deploy/release.sh --dry-run  check everything, publish nothing
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."
source "$SCRIPT_DIR/images.sh"
REPO=Bacinac/baba

DRY_RUN=0
case "${1:-}" in
    "") ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
esac

die() { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }
ok()  { printf '\033[32m✓ %s\033[0m\n' "$*"; }

[[ -z "$(git status --porcelain)" ]] || die "uncommitted changes — a release is exactly what origin/main holds"
git fetch -q --tags origin main
SHA=$(git rev-parse HEAD)
[[ "$SHA" == "$(git rev-parse origin/main)" ]] || die "HEAD is not origin/main"
VERSION="$(tr -d ' \t\n\r' < VERSION).$(git rev-list --count HEAD)"
TAG="v$VERSION"

[[ "$(git ls-remote origin "refs/tags/$TAG^{}" | cut -f1)" == "$SHA" ]] \
    || die "origin has no tag $TAG on ${SHA:0:8} — deploy/publish.sh tags the commit when it pushes the images"
gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1 && die "release $TAG already exists"

# Everything below is asked anonymously, as the stranger running install.sh would.
public_commit() { curl -fsS -o /dev/null "https://api.github.com/repos/$1/commits/$2"; }
public_commit "$REPO" "$SHA" || die "$REPO@${SHA:0:8} is not publicly readable"
while read -r key url; do
    name=${key#submodule.}; name=${name%.url}
    path=$(git config -f .gitmodules "submodule.$name.path")
    pin=$(git ls-tree HEAD "$path" | awk '{print $3}')
    [[ "$url" =~ ^https://github\.com/([^/]+/[^/]+)$ ]] || die "submodule $path: $url is not a public https URL"
    public_commit "${BASH_REMATCH[1]%.git}" "$pin" || die "submodule $path@${pin:0:8} is not publicly readable"
done < <(git config -f .gitmodules --get-regexp '\.url$')
ok "$REPO@${SHA:0:8} and its submodules are public"

REGISTRY="${IMAGE_BASE%%/*}"
NAMESPACE="${IMAGE_BASE#*/}"
MANIFESTS="application/vnd.oci.image.index.v1+json,application/vnd.oci.image.manifest.v1+json,application/vnd.docker.distribution.manifest.list.v2+json,application/vnd.docker.distribution.manifest.v2+json"
pullable() {
    local token
    token=$(curl -fsS "https://$REGISTRY/token?scope=repository:$NAMESPACE/$1:pull" | sed -n 's/.*"token":"\([^"]*\)".*/\1/p')
    [[ -n "$token" ]] && curl -fsS -o /dev/null -I -H "Authorization: Bearer $token" -H "Accept: $MANIFESTS" \
        "https://$REGISTRY/v2/$NAMESPACE/$1/manifests/$2"
}
missing=()
for variant in "${ALL_VARIANTS[@]}"; do
    for svc in "${SERVICES[@]}"; do
        pullable "$svc" "$variant-$VERSION" || missing+=("$svc:$variant-$VERSION")
    done
done
for image in "${COMMON_IMAGES[@]}"; do
    read -r repo alias <<< "$image"
    pullable "$repo" "$alias-$VERSION" || missing+=("$repo:$alias-$VERSION")
done
(( ${#missing[@]} == 0 )) || die "not pullable anonymously from $IMAGE_BASE: ${missing[*]}"
ok "every image of $TAG is pullable anonymously"

PREVIOUS=$(git describe --tags --abbrev=0 --match 'v[0-9]*' "$SHA^" 2>/dev/null || true)
if [[ -n "$PREVIOUS" ]]; then
    NOTES=$(git log --no-merges --format='- %s' "$PREVIOUS..$SHA")
else
    NOTES="First public release."
fi

if (( DRY_RUN )); then
    printf 'would create release %s on %s with these notes:\n%s\n' "$TAG" "$REPO" "$NOTES"
    exit 0
fi
gh release create "$TAG" --repo "$REPO" --verify-tag --latest --title "BABA $TAG" --notes "$NOTES" >/dev/null
ok "released $TAG — rebuild and deploy boskovic.biz to show it"
