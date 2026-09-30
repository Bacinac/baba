#!/usr/bin/env bash
# Build the public, backend-less BABA demo (static SPA).
#
# Same frontend as production; only its network layer is swapped (static/demo-net.js
# patches fetch + WebSocket) and it is emitted as a static SPA. The grid's polled
# camera stills are served as REAL FILES — the hand-reviewed, blurred stills — so
# every pixel a visitor sees is anonymised. Drill-in video (stream.mp4) needs the
# blurred loop clips (added separately); until then it falls back to the poster.
#
#   web/build-demo.sh <scrubbed-fixtures.json> [out-dir] [stills-dir]
set -euo pipefail

FIXTURES="${1:?usage: build-demo.sh <scrubbed-fixtures.json> [out-dir] [stills-dir]}"
WEB="$(cd "$(dirname "$0")" && pwd)"
OUT="${2:-$WEB/demo-dist}"
STILLS="${3:-$WEB/../demo/stills}"

[[ -f "$FIXTURES" ]] || { echo "no fixtures at $FIXTURES" >&2; exit 1; }

cd "$WEB"
trap 'rm -f static/demo-fixtures.json' EXIT
cp "$FIXTURES" static/demo-fixtures.json

# Merge synthetic demo content (Scene-states example regions) into the fixtures.
# Keyed by camera SLUG in demo-extras.json; resolved to camera_id here so the
# tracked template carries no instance data. Runs BEFORE the privacy gate so the
# synthetic rows are scanned too. Skips gracefully if the file is absent.
EXTRAS="$WEB/../demo/demo-extras.json"
if [[ -f "$EXTRAS" ]]; then
  python3 - static/demo-fixtures.json "$EXTRAS" <<'PY'
import json, sys
fx_path, ex_path = sys.argv[1], sys.argv[2]
fx = json.load(open(fx_path))
ex = json.load(open(ex_path))
cams = fx.get("/cameras", [])
by_slug = {c.get("slug"): c.get("id") for c in (cams if isinstance(cams, list) else [])}
n = 0
for slug, regions in ex.get("scene_regions", {}).items():
    cid = by_slug.get(slug)
    if not cid:
        print(f"  ! scene-regions: no camera slug {slug!r} in fixtures", file=sys.stderr); continue
    for r in regions:
        r["camera_id"] = cid
        # Prototype thumbnails: point crop_path at a real per-prototype file that
        # the post-build crop step writes; strip crop_bbox (a build-only hint,
        # not an API field) so the served fixture stays clean.
        for pr in r.get("prototypes", []):
            pr["crop_path"] = f"crops/scene/{pr['id']}.jpg"
            pr.pop("crop_bbox", None)
    fx[f"/cameras/{cid}/scene-regions"] = regions
    n += len(regions)
json.dump(fx, open(fx_path, "w"), ensure_ascii=False)
print(f"merged {n} scene regions")
PY
fi

# Fail-CLOSED: refuse to build if any secret / IP / real name survived the scrub.
python3 "$WEB/../demo/privacy_gate.py" static/demo-fixtures.json
# The same deny list every public push answers to (the live household, plates,
# MACs, secrets), found through the clone's own config: required, not optional.
GATE=$(git -C "$WEB" config --get publicgate.command) \
    || { echo "no publicgate.command in this clone — the demo is not built unchecked" >&2; exit 1; }
find "$STILLS" -type f -print0 2>/dev/null | xargs -0 "$GATE" scan --files static/demo-fixtures.json

# Inject the shim into the placeholder app.html carries for exactly this purpose.
cp src/app.html src/app.html.bak
trap 'mv -f src/app.html.bak src/app.html; rm -f static/demo-fixtures.json' EXIT
sed -i 's|<!--DEMO_NET-->|<script src="/demo-net.js"></script>|' src/app.html

WIMG=$(docker build -q --target deps -f "$WEB/Dockerfile" "$WEB/..")
rm -rf "$OUT"
mkdir -p "$OUT"
docker run --rm -e BABA_DEMO=1 -e VITE_BABA_DEMO=1 -e npm_config_update_notifier=false \
  -v "$WEB:/w:ro" -v "$OUT:/out" --entrypoint sh "$WIMG" -c \
  "tar -C /w -c --exclude=./node_modules --exclude=./build --exclude=./demo-dist --exclude=./.svelte-kit . | tar -x -C /app \
   && npx svelte-kit sync && npx vite build && cp -r build/. /out/ && chown -R $(id -u):$(id -g) /out"

# Camera grid stills: LiveTile polls /api/cameras/<id>/live.jpg. Map each camera's
# id (from the recorded /cameras fixture) to its blurred still (named by slug) and
# write it as a REAL FILE at that exact path — the demo-net.js passthrough lets the
# request reach the static server. Also drop it at /snapshot for poster frames.
python3 - "$FIXTURES" "$STILLS" "$OUT" <<'PY'
import json, sys, shutil, pathlib
fixtures, stills, out = sys.argv[1], pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3])
cams = json.load(open(fixtures)).get("/cameras", [])
n = 0
for c in cams if isinstance(cams, list) else []:
    cid, slug = c.get("id"), c.get("slug")
    src = stills / f"{slug}.jpg"
    if not (cid and src.exists()):
        print(f"  ! camera {slug!r} (id {cid}): no still", file=sys.stderr); continue
    for leaf in ("live.jpg", "snapshot"):
        d = out / "api" / "cameras" / str(cid)
        d.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, d / leaf)
    # Slug-addressable copy for LiveStream's demo poster (drill-in + zone editor
    # size/point at the still by slug, not camera id).
    shutil.copy(src, out / f"demo-still-{slug}.jpg")
    n += 1
print(f"wired {n} camera stills")
PY

# Scene-state prototype thumbnails: crop each declared crop_bbox out of the
# camera's blurred still into /api/crops/scene/<proto-id>.jpg (a REAL file, so it
# wins over the /api/crops/* -> demo-crop.jpg fallback). Makes the reference
# thumbnails show actual (blurred) imagery per state — a trained-looking region.
if [[ -f "$EXTRAS" ]]; then
  python3 - "$FIXTURES" "$EXTRAS" "$STILLS" "$OUT" <<'PY'
import json, sys, pathlib
from PIL import Image
fixtures, ex_path, stills, out = sys.argv[1], sys.argv[2], pathlib.Path(sys.argv[3]), pathlib.Path(sys.argv[4])
cams = json.load(open(fixtures)).get("/cameras", [])
by_slug = {c.get("slug"): c for c in (cams if isinstance(cams, list) else [])}
ex = json.load(open(ex_path))
dst = out / "api" / "crops" / "scene"
dst.mkdir(parents=True, exist_ok=True)
n = 0
for slug, regions in ex.get("scene_regions", {}).items():
    src = stills / f"{slug}.jpg"
    if not src.exists():
        print(f"  ! scene crops: no still for {slug!r}", file=sys.stderr); continue
    im = Image.open(src).convert("RGB")
    W, H = im.size
    for r in regions:
        for pr in r.get("prototypes", []):
            bb = pr.get("crop_bbox")
            if not bb:
                continue
            x1, y1, x2, y2 = bb
            box = (int(x1 * W), int(y1 * H), int(x2 * W), int(y2 * H))
            crop = im.crop(box)
            crop.thumbnail((320, 320))
            crop.save(dst / f"{pr['id']}.jpg", "JPEG", quality=82)
            n += 1
print(f"cropped {n} scene prototypes")
PY
fi

# Cloudflare Pages: serve the still bytes as image/jpeg (the paths carry no ext).
printf '/api/cameras/*\n  Content-Type: image/jpeg\n' > "$OUT/_headers"

# Media the app loads via <img>/<video> bypass the fetch shim -> resolve on the
# static server. Person crops = close-ups of faces -> ONE neutral tile. Each
# camera's recorded clip + live stream -> ITS OWN hand-reviewed blurred clip
# (an Activity replay must show the camera it fired on, not one shared clip).
# CF Pages _redirects rewrites the dynamic paths; the SPA catch-all is last and
# never shadows an existing static file.
cp "$WEB/../demo/media/demo-crop.jpg" "$OUT/demo-crop.jpg"
python3 - "$FIXTURES" "$WEB/../demo/media" "$OUT" <<'PYX'
import json, sys, shutil, pathlib
fixtures, media, out = sys.argv[1], pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3])
cams = json.load(open(fixtures)).get("/cameras", [])
lines = ["/api/thumbnails/*          /demo-crop.jpg   200",
         "/api/crops/*               /demo-crop.jpg   200",
         "/api/reference_photos/*    /demo-crop.jpg   200"]
for c in cams if isinstance(cams, list) else []:
    cid, slug = c.get("id"), c.get("slug")
    clip = media / f"demo-clip-{slug}.mp4"
    if cid and slug and clip.exists():
        shutil.copy(clip, out / f"demo-clip-{slug}.mp4")
        lines.append(f"/api/recordings/cameras/{cid}/clip*  /demo-clip-{slug}.mp4  200")
# generic fallbacks + SPA catch-all last
if (media / "demo-clip.mp4").exists():
    shutil.copy(media / "demo-clip.mp4", out / "demo-clip.mp4")
lines += ["/api/recordings/cameras/*  /demo-clip.mp4   200",
          "/api/stream.mp4            /demo-clip.mp4   200",
          "/*                         /index.html      200"]
(out / "_redirects").write_text(chr(10).join(lines) + chr(10))
print(f"wired {sum(1 for l in lines if 'demo-clip-' in l)} per-camera clips")
PYX
echo "demo built -> $OUT"
