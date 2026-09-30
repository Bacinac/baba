#!/usr/bin/env bash
# export-tree.sh <commit> — that commit's tree as one tar on stdout, its
# submodules included.
#
# `git archive` stops at a submodule: it writes the directory and nothing in
# it, so web/src/lib/kit would arrive empty — a gate that checks nothing and an
# instance that cannot build its web. Each submodule is archived at the commit
# this one records and appended under its path, so a plain `tar -x` unpacks
# the whole of it.
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel)"
sha="$1"
out=$(mktemp)
trap 'rm -f "$out" "$out.sub"' EXIT
git -C "$ROOT" archive --format=tar "$sha" >"$out"
while read -r sub path; do
    git -C "$ROOT/$path" archive --format=tar --prefix="$path/" "$sub" >"$out.sub"
    tar -A -f "$out" "$out.sub"
done < <(git -C "$ROOT" ls-tree -r "$sha" | awk '$2 == "commit" { print $3, $4 }')
cat "$out"
