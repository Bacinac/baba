"""Held-out measurement of the face-anchored body chain threshold.

    docker compose exec -T api python - < tools/measure_face_anchor_threshold.py

The chain promotes an anonymous track to a named identity when its body
matches a recent FACE-VERIFIED track of that identity on the same camera.
`reid_face_anchor_body_threshold` decides how close is close enough, and
getting it wrong is asymmetric: too tight and the chain never fires, too loose
and a stranger inherits somebody's name.

Ground truth here is face verification, the only label not derived from body
appearance. Each face-verified track is held out in turn, treated as if it were
anonymous, and offered the anchors the live rule would have seen: other
face-verified tracks, same camera, inside the window, strictly earlier. The
nearest one wins, exactly as in production. Sweeping the threshold over that
then gives the two numbers that matter — how many arrivals get named, and how
many get named as the WRONG person.

The cross-identity pair distribution is printed separately because it is what
bounds the top: without strangers in the sample, a threshold can be shown to
help and still be unsafe.
"""

from __future__ import annotations

import asyncio
import os
from collections import defaultdict

import asyncpg
import numpy as np

WINDOW_HOURS = int(os.environ.get("BABA_EVENT_REID_FACE_ANCHOR_WINDOW_HOURS", "6"))
LIVE_THRESHOLD = float(
    os.environ.get("BABA_EVENT_REID_FACE_ANCHOR_BODY_THRESHOLD", "0.25")
)
SWEEP = [0.20, 0.25, 0.30, 0.32, 0.35, 0.38, 0.40, 0.45, 0.50]


def _vec(raw: str) -> np.ndarray:
    return np.fromstring(raw.strip("[]"), sep=",", dtype=np.float32)


def _cos(a: np.ndarray, b: np.ndarray) -> float:
    return float(1.0 - np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))


async def main() -> None:
    c = await asyncpg.connect(
        host=os.environ["POSTGRES_HOST"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        database=os.environ["POSTGRES_DB"],
    )
    rows = await c.fetch(
        """
        SELECT t.id, cam.slug AS cam, il.name AS ident, t.started_at,
               t.embedding::text AS emb
        FROM tracks t
        JOIN cameras cam ON cam.id = t.camera_id
        JOIN identity_labels il ON il.global_id = t.global_id
        WHERE t.face_verified AND t.embedding IS NOT NULL AND t.class_id = 0
        ORDER BY t.started_at
        """
    )
    await c.close()

    tracks = [
        {
            "id": r["id"],
            "cam": r["cam"],
            "ident": r["ident"],
            "at": r["started_at"],
            "v": _vec(r["emb"]),
        }
        for r in rows
    ]
    print(f"ground truth: {len(tracks)} face-verified person tracks with a body vector")
    per_cam: dict[str, list[dict]] = defaultdict(list)
    for t in tracks:
        per_cam[t["cam"]].append(t)
    for cam, ts in sorted(per_cam.items()):
        names = defaultdict(int)
        for t in ts:
            names[t["ident"]] += 1
        print(f"  {cam:<10} {dict(names)}")

    same_all: list[float] = []
    cross_all: list[float] = []
    same_win: list[float] = []
    cross_win: list[float] = []
    win = WINDOW_HOURS * 3600
    for ts in per_cam.values():
        for i in range(len(ts)):
            for j in range(i + 1, len(ts)):
                d = _cos(ts[i]["v"], ts[j]["v"])
                gap = abs((ts[j]["at"] - ts[i]["at"]).total_seconds())
                same = ts[i]["ident"] == ts[j]["ident"]
                (same_all if same else cross_all).append(d)
                if gap <= win:
                    (same_win if same else cross_win).append(d)

    def describe(label: str, xs: list[float]) -> None:
        if not xs:
            print(f"{label:<34} n=0")
            return
        a = np.array(xs)
        print(
            f"{label:<34} n={len(xs):<4} min={a.min():.3f} p25={np.percentile(a, 25):.3f} "
            f"median={np.median(a):.3f} p75={np.percentile(a, 75):.3f} max={a.max():.3f}"
        )

    print(f"\n=== pair distances, same camera (window = {WINDOW_HOURS}h) ===")
    describe("ista osoba, bilo kada", same_all)
    describe("razlicite osobe, bilo kada", cross_all)
    describe(f"ista osoba, unutar {WINDOW_HOURS}h", same_win)
    describe(f"razlicite osobe, unutar {WINDOW_HOURS}h", cross_win)

    # Held-out: each track pretends to be anonymous and takes the nearest
    # earlier anchor on its camera, exactly as the live rule does.
    probes: list[tuple[float, bool]] = []
    no_anchor = 0
    for ts in per_cam.values():
        for k, probe in enumerate(ts):
            anchors = [
                a
                for a in ts[:k]
                if 0 < (probe["at"] - a["at"]).total_seconds() <= win
            ]
            if not anchors:
                no_anchor += 1
                continue
            best = min(anchors, key=lambda a: _cos(probe["v"], a["v"]))
            probes.append((_cos(probe["v"], best["v"]), best["ident"] == probe["ident"]))

    print(f"\n=== held-out: {len(probes)} probes with an anchor, {no_anchor} without ===")
    if not probes:
        print("nema nijednog probe-a s anchorom — mjerenje nije moguce")
        return
    print(f"{'prag':>6} {'imenovano tocno':>16} {'imenovano KRIVO':>16} {'ostalo anonimno':>16}")
    for tau in SWEEP:
        ok = sum(1 for d, corr in probes if d < tau and corr)
        bad = sum(1 for d, corr in probes if d < tau and not corr)
        anon = len(probes) - ok - bad
        mark = "  <- zivi prag" if abs(tau - LIVE_THRESHOLD) < 1e-9 else ""
        print(f"{tau:>6.2f} {ok:>16} {bad:>16} {anon:>16}{mark}")


if __name__ == "__main__":
    asyncio.run(main())
