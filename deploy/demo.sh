#!/usr/bin/env bash
# Rebuild and publish the public, backend-less demo at https://demo-baba.boskovic.biz.
#
# It is the REAL frontend at HEAD, built as a static SPA with its network layer
# swapped (web/static/demo-net.js) and fed the last recorded, anonymised
# fixtures. deploy.sh runs it after deploying an instance flagged `demo`, so the
# demo always shows the frontend that just shipped:
#   ./deploy/demo.sh
#
# The data is NOT re-recorded here. Recording signs in to production with a
# password only the owner types (demo/record_baba.py -> demo/scrub_fixtures.py
# -> demo/anon-fixtures.json) and every still is reviewed by hand; this script
# carries the current frontend onto that recording, nothing more.
#
# Both privacy gates run inside web/build-demo.sh and refuse the build itself.
# The tree must be committed: the public demo must never show code that has not
# been through the gate every push goes through.
#
# Needs: demo/anon-fixtures.json + demo/stills, ./.cf-token (Cloudflare
# Pages:Edit), docker.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT="${BABA_DEMO_PROJECT:-baba-demo}"
FIXTURES="$ROOT/demo/anon-fixtures.json"
CF_TOKEN_FILE="${BABA_CF_TOKEN:-$ROOT/.cf-token}"
CF_ACCOUNT="${CLOUDFLARE_ACCOUNT_ID:-3e7a41dadfd065fac67c6922c3f3b43e}"

die() { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

[[ -s "$FIXTURES" ]] || die "no recorded fixtures at $FIXTURES"
[[ -s "$CF_TOKEN_FILE" ]] || die "no Cloudflare token at $CF_TOKEN_FILE"
git -C "$ROOT" diff --quiet HEAD -- web demo \
    || die "uncommitted changes under web/ or demo/ — the demo shows committed code only"
[[ -z "$(git -C "$ROOT" ls-files --others --exclude-standard -- web)" ]] \
    || die "untracked files under web/ would be built into the demo"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "== building the demo at $(git -C "$ROOT" rev-parse --short HEAD) =="
"$ROOT/web/build-demo.sh" "$FIXTURES" "$WORK/site"

echo "== deploying to $PROJECT =="
# Through an env file in the private work dir, not -e: an argument is visible
# to every process listing for as long as the container runs.
printf 'CLOUDFLARE_API_TOKEN=%s\nCLOUDFLARE_ACCOUNT_ID=%s\n' \
    "$(tr -d ' \n' < "$CF_TOKEN_FILE")" "$CF_ACCOUNT" > "$WORK/cf.env"
out=$(docker run --rm --env-file "$WORK/cf.env" -e npm_config_update_notifier=false \
        -v "$WORK/site":/site:ro node:24 \
        sh -c "npx -y wrangler@latest pages deploy /site --project-name $PROJECT --branch main 2>&1") \
    || { printf '%s\n' "$out" | tail -20 >&2; die "wrangler refused the deploy"; }
printf '%s\n' "$out" | grep 'Deployment complete' \
    || { printf '%s\n' "$out" | tail -20 >&2; die "wrangler reported no deployment"; }
