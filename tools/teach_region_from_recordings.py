"""Teach a scene region from footage it has already recorded.

A region classifies against a handful of DINOv2 prototypes, and every one of
this system's place regions was taught in a single sitting in daylight — west
P1 owned exactly one example of an empty spot, shed P1 three, all from one July
morning.

    python teach_region_from_recordings.py west P4 empty 2026-09-02@21:13,2026-09-03@04:30

Check the footage before you name it. The registry is not proof: on 02.-03.09
west P3 stood over a red car all night that no episode knew about, and teaching
those frames as `empty` would have been the exact mistake this tool makes easy. Under IR they are choosing between two things they have never seen,
which is where the twenty phantom nightly occupancies came from, and why shed
P1 sat at `present` across a real departure.

Nothing had to be waited for: the recorder keeps every camera 24/7, and P1
stood empty from 31.07 to 16.08. The evaluator can already embed a region from
a recorded moment (`baba.rpc.state.capture` with `at`), so the prototypes are
harvested from the days that are already on disk.
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

LOCAL = ZoneInfo("Europe/Zagreb")


async def main():
    slug, region_name, label, spec = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
    pool = await asyncpg.create_pool(
        host=os.environ["POSTGRES_HOST"], user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"], database=os.environ["POSTGRES_DB"],
        min_size=1, max_size=2)
    rid = await pool.fetchval(
        "SELECT sr.id FROM scene_regions sr JOIN cameras c ON c.id=sr.camera_id"
        " WHERE c.slug=$1 AND sr.name=$2", slug, region_name)
    assert rid, f"no region {slug}/{region_name}"

    # `YYYY-MM-DD@hour` lands on the half hour; `@hour:minute` when the state
    # you want lasts minutes rather than hours (a car that stood for half an
    # hour has no :30 to be sampled at), `@hour:minute:second` when it lasts
    # seconds (the gate stands open for twenty). The date is spelled out because the
    # month used to be a literal in this file, which quietly aimed every
    # harvest at August from the moment September started.
    moments = []
    for day_hour in spec.split(","):
        day, hm = day_hour.split("@")
        y, mo, d = (int(x) for x in day.split("-"))
        h, _, ms = hm.partition(":")
        m, _, sec = ms.partition(":")
        moments.append(datetime(y, mo, d, int(h), int(m or 30), int(sec or 0), tzinfo=LOCAL))

    nc = await nats.connect(os.environ.get("BABA_NATS_URL", "nats://nats:4222"), name="harvest")
    ok = bad = 0
    for at in moments:
        req = {"region_id": str(rid), "state_label": label,
               "at": at.astimezone(UTC).isoformat()}
        try:
            resp = await nc.request(SUBJECT_STATE_CAPTURE, json.dumps(req).encode(), timeout=60)
            r = json.loads(resp.data)
        except (nats.errors.Error, ValueError) as e:
            print(f"  {at:%d.%m %H:%M}  request failed: {e}")
            bad += 1
            continue
        if r.get("error"):
            print(f"  {at:%d.%m %H:%M}  {r['error']}")
            bad += 1
        else:
            ok += 1
            print(f"  {at:%d.%m %H:%M}  ok  {r['crop_path']}")
    print(f"{slug}/{region_name} {label}: {ok} captured, {bad} failed")
    await nc.drain()
    await pool.close()


asyncio.run(main())
