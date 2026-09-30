"""Measure what a camera's floodlight brightness actually buys, step by step.

The value on west was never chosen — it is 10, the others in the yard are 85 to
100, and nobody had measured what that costs. It pulls two ways at once:

  too bright  the plate zone sits at 196 of 255 (26.08, 21:15). Nothing has
              headroom there — not a registration, and not the headlight rule,
              which asks whether the zone has DOUBLED and cannot be satisfied
              above 128.
  too dim     43, and the camera falls out of colour into monochrome (28.08,
              same minute), which is both the picture the operator does not want
              and the state in which a headlight owns the whole zone.

So the answer is the lowest brightness that keeps the picture in colour and
leaves the plate zone with room above it. This walks the range after dark,
reading each step off the live frames, and puts the camera back where it was.

Run it inside a container that can see the frame ring and the api:

    KEY=$(docker exec baba-api printenv BABA_PEER_KEYS | cut -d: -f2)
    docker exec -i -e BABA_PEER_KEY=$KEY baba-state-evaluator \
        python3 - west < tools/sweep_floodlight.py

Every write goes through the api's light endpoint, never at the device: one
writer to a camera is the whole point of that layer.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.request

import numpy as np

sys.path.insert(0, "/app/src")

from baba_core.dsn import dsn_from_env
from baba_core.frame_ring import FrameRingReader

STEPS = [10, 20, 30, 40, 60, 80, 100]

# The camera's auto-exposure chases a brightness change for several seconds, and
# reading during that measures the chase rather than the setting.
SETTLE_S = 20.0
SAMPLES = 5
SAMPLE_GAP_S = 2.0

# Below this mean |chroma - 128| the picture is monochrome. The ingestor's own
# threshold, so "in colour" means here exactly what it means in the telemetry.
IR_CHROMA_DEV = 2.0

# The api by its service name, because this runs in the container that can see
# the frame ring — which is not the container that owns the camera.
API = os.environ.get("BABA_API_URL", "http://api:8080")
PEER_KEY = os.environ.get("BABA_PEER_KEY", "")


def _light(camera_id: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"{API}/cameras/{camera_id}/light",
        headers={"X-Peer-Key": PEER_KEY, "Content-Type": "application/json"},
        data=json.dumps(body).encode() if body else None,
        method="POST" if body else "GET",
    )
    return json.loads(urllib.request.urlopen(req, timeout=20).read())


def _measure(reader: FrameRingReader, box) -> tuple[float, float, float] | None:
    """Zone luma, whole-frame luma, and how far the chroma is from neutral."""
    frame = reader.get_latest()
    if frame is None or frame.pixels.ndim != 2:
        return None
    h, w = frame.height, frame.width
    y = frame.pixels[:h]
    uv = frame.pixels[h:]
    x1, y1, x2, y2 = box
    zone = y[int(y1 * h):int(y2 * h), int(x1 * w):int(x2 * w)]
    chroma = float(np.abs(uv[::4, ::4].astype(np.int16) - 128).mean())
    return float(zone.mean()), float(y.mean()), chroma


async def main(slug: str) -> None:
    import asyncpg

    pool = await asyncpg.create_pool(dsn_from_env(), min_size=1, max_size=2)
    row = await pool.fetchrow(
        """
        SELECT c.id::text AS id, json_agg(z.polygon) AS polygons
        FROM cameras c LEFT JOIN zones z
          ON z.camera_id = c.id AND z.kind = 'alpr' AND z.enabled
        WHERE c.slug = $1 GROUP BY c.id
        """,
        slug,
    )
    if row is None:
        raise SystemExit(f"no camera {slug!r}")
    polys = row["polygons"]
    polys = json.loads(polys) if isinstance(polys, str) else polys
    polys = [p for p in (polys or []) if p]
    if polys:
        pts = [pt for p in polys for pt in p]
        box = (min(p[0] for p in pts), min(p[1] for p in pts),
               max(p[0] for p in pts), max(p[1] for p in pts))
    else:
        box = (0.0, 0.0, 1.0, 1.0)

    before = _light(row["id"])
    print(f"{slug}: light {before}", flush=True)
    if not before.get("on"):
        raise SystemExit("the lamp is not armed — nothing to sweep")
    reader = FrameRingReader(slug)

    print(f"\n{'bright':>7} {'zone':>7} {'frame':>7} {'chroma':>7}  picture", flush=True)
    try:
        for bright in STEPS:
            _light(row["id"], {"on": True, "bright": bright})
            await asyncio.sleep(SETTLE_S)
            reads = []
            for _ in range(SAMPLES):
                m = _measure(reader, box)
                if m:
                    reads.append(m)
                await asyncio.sleep(SAMPLE_GAP_S)
            if not reads:
                print(f"{bright:>7} no frames", flush=True)
                continue
            zone = sum(r[0] for r in reads) / len(reads)
            whole = sum(r[1] for r in reads) / len(reads)
            chroma = sum(r[2] for r in reads) / len(reads)
            print(f"{bright:>7} {zone:>7.1f} {whole:>7.1f} {chroma:>7.2f}  "
                  f"{'colour' if chroma >= IR_CHROMA_DEV else 'mono'}", flush=True)
    finally:
        # Whatever happened, the yard goes back the way its owner left it.
        _light(row["id"], {"on": True, "bright": before["bright"]})
        print(f"\nrestored to {before['bright']}", flush=True)
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "west"))
