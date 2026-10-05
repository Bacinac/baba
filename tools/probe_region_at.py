"""Score a scene region on frames it has already recorded, without committing.

`baba.rpc.state.eval` answers for the live frame only, and `scene_region_status`
keeps one distance — the latest. Neither can say what a region THOUGHT at a
past moment, which is exactly the question a phantom transition raises: west P4
released a place at 21:02 and the row said `empty`, but not with what score, or
whether the next tick would have said the same.

So: capture through the real path (`baba.rpc.state.capture` with `at` — same
decode, same crop+mask, same DINOv2 as the evaluator), score the vector against
the region's live prototypes, print the per-state nearest, and DELETE the probe
so the prototype set is exactly as it was. Reads nothing the evaluator would
not have read.

    python probe_region_at.py west P4 "2026-09-07@20:55,2026-09-07@21:02"

The date is spelled out per moment for the same reason the harvester spells it
out: the month was a literal here too, so from September on every probe silently
scored August footage and answered about a day nobody asked for.
"""
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import asyncpg
import nats
from baba_core.paths import StoragePaths
from baba_core.wire import SUBJECT_STATE_CAPTURE
from baba_state_evaluator.__main__ import verdict

LOCAL = ZoneInfo("Europe/Zagreb")
_PROBE = "__probe__"


async def main():
    slug, region_name, moments = sys.argv[1], sys.argv[2], sys.argv[3].split(",")
    pool = await asyncpg.create_pool(
        host=os.environ["POSTGRES_HOST"], user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"], database=os.environ["POSTGRES_DB"],
        min_size=1, max_size=2)
    row = await pool.fetchrow(
        "SELECT sr.id, sr.unknown_margin FROM scene_regions sr"
        " JOIN cameras c ON c.id = sr.camera_id WHERE c.slug=$1 AND sr.name=$2",
        slug, region_name)
    assert row, f"no region {slug}/{region_name}"
    rid, margin = row["id"], float(row["unknown_margin"])
    crops = StoragePaths.from_env().layout.scene_crops
    nc = await nats.connect(os.environ.get("BABA_NATS_URL", "nats://nats:4222"),
                            name="probe-region")
    print(f"{slug}/{region_name}  unknown_margin={margin:.2f}")
    for hm in moments:
        day_str, _, hm_str = hm.partition("@")
        y, mo, d = (int(x) for x in day_str.split("-"))
        h, _, ms = hm_str.partition(":")
        m, _, sec = ms.partition(":")
        at = datetime(y, mo, d, int(h), int(m or 0), int(sec or 0), tzinfo=LOCAL)
        req = {"region_id": str(rid), "state_label": _PROBE,
               "at": at.astimezone(UTC).isoformat()}
        r = json.loads(
            (await nc.request(SUBJECT_STATE_CAPTURE, json.dumps(req).encode(),
                              timeout=90)).data)
        if r.get("error"):
            print(f"  {hm}  {r['error']}")
            continue
        pid = r["prototype_id"]
        try:
            scored = await pool.fetch(
                """
                WITH me AS (SELECT embedding FROM scene_region_prototypes WHERE id=$1)
                SELECT p.state_label,
                       MIN(p.embedding <=> (SELECT embedding FROM me)) AS d
                FROM scene_region_prototypes p
                WHERE p.region_id = $2 AND p.id <> $1 AND p.state_label <> $3
                GROUP BY 1 ORDER BY 2
                """, pid, rid, _PROBE)
        finally:
            # Always, and in this order: the row goes, then the region is
            # touched so the evaluator reloads its prototypes from the table.
            # The capture path appends to the in-memory set too, and deleting
            # only the row once left three regions committing `__probe__` and
            # filing transitions for it. `_classify` now ignores any label the
            # region does not declare, so this is the second line of defence.
            await pool.execute(
                "DELETE FROM scene_region_prototypes WHERE id=$1", pid)
            await pool.execute(
                "UPDATE scene_regions SET updated_at = now() WHERE id=$1", rid)
            (crops / Path(r["crop_path"]).name).unlink(missing_ok=True)
        per = {s["state_label"]: float(s["d"]) for s in scored}
        best = min(per, key=per.get)
        print(f"  {hm}  " + "  ".join(f"{k}={v:.3f}" for k, v in per.items())
              + f"   -> {verdict(best, per[best], per, margin)}")
    await nc.drain()
    await pool.close()


asyncio.run(main())
