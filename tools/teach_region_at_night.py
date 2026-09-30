"""Capture the night `empty` prototypes P2 has never had, when it really is empty.

Every place region was taught in daylight, so under IR they classify against
two references neither of which they have ever seen — which is where twenty
phantom nightly occupancies came from. For P1, P3 and P4 the gap was filled
from recordings: they stood empty for days and the recorder kept every frame.
P2 could not be: it has been occupied for all but four hours of the footage
that still exists, and none of those hours were dark.

So this waits for the dark instead. It refuses to capture unless the registry
says the place is free AND no vehicle has been tracked near it recently — a
crop of a car labelled `empty` is worse than no crop at all, and the whole
reason this file exists is that a region taught the wrong thing is very hard
to notice afterwards.
"""
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import asyncpg
import nats
from baba_core.wire import SUBJECT_STATE_CAPTURE

PLACE = sys.argv[1] if len(sys.argv) > 1 else "P2"
MAX_NIGHT_PROTOS = 6
# What this exists to collect. Run in daylight it would quietly fill the quota
# with references the region already has plenty of, and the gap it was written
# to close would still be there.
NIGHT_HOURS = set(range(21, 24)) | set(range(0, 6))


async def main():
    pool = await asyncpg.create_pool(
        host=os.environ["POSTGRES_HOST"], user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"], database=os.environ["POSTGRES_DB"],
        min_size=1, max_size=2)
    local = datetime.now(UTC).astimezone(ZoneInfo("Europe/Zagreb"))
    stamp = local.strftime("%d.%m %H:%M")
    if local.hour not in NIGHT_HOURS:
        print(f"{stamp} {PLACE}: not dark — nothing to collect")
        await pool.close()
        return

    occupied = await pool.fetchval(
        "SELECT EXISTS(SELECT 1 FROM place_occupancy WHERE place=$1 AND released_at IS NULL)",
        PLACE)
    if occupied:
        print(f"{stamp} {PLACE}: registry says occupied — nothing to capture")
        await pool.close()
        return
    recent_vehicle = await pool.fetchval(
        "SELECT EXISTS(SELECT 1 FROM tracks WHERE class_id = ANY('{2,3,5,7}') "
        "AND ended_at > now() - interval '20 minutes')")
    if recent_vehicle:
        print(f"{stamp} {PLACE}: a vehicle was tracked in the last 20 min — not now")
        await pool.close()
        return

    regions = await pool.fetch(
        """
        SELECT sr.id, c.slug,
               (SELECT count(*) FROM scene_region_prototypes p
                 WHERE p.region_id = sr.id AND p.state_label = 'empty'
                   AND extract(hour FROM p.captured_at AT TIME ZONE 'Europe/Zagreb')
                       NOT BETWEEN 6 AND 20) AS night_empty
        FROM scene_regions sr JOIN cameras c ON c.id = sr.camera_id
        WHERE sr.place = $1 AND sr.enabled
        """, PLACE)

    nc = await nats.connect(os.environ.get("BABA_NATS_URL", "nats://nats:4222"), name="teach")
    for r in regions:
        if r["night_empty"] >= MAX_NIGHT_PROTOS:
            print(f"{stamp} {r['slug']}/{PLACE}: already has {r['night_empty']} night refs")
            continue
        req = {"region_id": str(r["id"]), "state_label": "empty"}
        resp = await nc.request(SUBJECT_STATE_CAPTURE, json.dumps(req).encode(), timeout=60)
        out = json.loads(resp.data)
        print(f"{stamp} {r['slug']}/{PLACE}: "
              + (out["error"] if out.get("error") else f"captured {out['crop_path']}"))
    await nc.drain()
    await pool.close()


asyncio.run(main())
