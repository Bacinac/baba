"""Rebuild tracks the old suppression threw away, from the evidence it left.

Until `100_a_filter_may_hide_a_track_never_delete_it`, a zone or static verdict
returned out of finalize before the row was written. The embedder had already
done its work by then: every crop it accepted is still in
`track_embedding_samples`, with its vector, its box and its timestamp, and with
`track_id` NULL forever because the finalize that would have claimed it never
completed. A man carrying a parcel through the gate on 11.09 left fourteen of
them, three with a face, and not one row anywhere.

So the samples ARE the record; this reassembles a track row from them, the same
shape finalize would have written. What it cannot reconstruct is the zone
verdict (the per-frame zone membership is gone), so it only restores tracks
whose class is attested by an event they emitted while alive — and by default
only `person`, the class the new rule says a dwell threshold may never hide.

Run it from the instance's checkout. The images carry the services, not this
directory, so the tools dir is mounted in:

    docker compose run --rm -T -e TZ=Europe/Zagreb \
        -v /mnt/docker/baba/tools:/tools:ro \
        --entrypoint /opt/venv/bin/python3 event-manager \
        /tools/recover_lost_tracks.py --camera gate --local-id 2

Timestamps print in the container's zone and name it, so a missing TZ shows as
UTC rather than passing for local time.

Dry by default; `--apply` writes. Rerunning is safe: a track whose samples have
been claimed no longer looks lost.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
from baba_core import dsn_from_env, go2rtc_auth_from_env
from baba_core.retention import ANONYMOUS
from baba_event_manager.thumbs import ThumbnailCapture

log = logging.getLogger("recover")

# Same floor finalize applies before it writes anything: below this a track is
# a detector flicker, not a subject. Samples are sparser than observations, so
# five of them is well clear of `BABA_EVENT_MIN_OBSERVATIONS`.
MIN_SAMPLES = 5
MIN_SPAN_S = 1.5

# Local track ids are reused after a tracker restart, so (camera, local id) is
# not a key. A gap this long between samples means a different subject.
GROUP_GAP = "2 minutes"

# A coasting tracker repeats the last real detection's confidence frame after
# frame while the subject is already gone (the parked-ghost latch). Those
# samples are emission, not presence — the same distinction `last_evidence_ns`
# draws in finalize — so the visit ends at the last sample that saw something
# new.
_LOST_SQL = f"""
WITH s AS (
    SELECT camera_id, local_track_id, captured_at,
           CASE WHEN captured_at - lag(captured_at) OVER w > interval '{GROUP_GAP}'
                  OR lag(captured_at) OVER w IS NULL THEN 1 ELSE 0 END AS brk
      FROM track_embedding_samples
     WHERE track_id IS NULL
       AND ($1::uuid IS NULL OR camera_id = $1)
       AND ($2::int  IS NULL OR local_track_id = $2)
       AND ($3::timestamptz IS NULL OR captured_at >= $3)
    WINDOW w AS (PARTITION BY camera_id, local_track_id ORDER BY captured_at)
), g AS (
    SELECT camera_id, local_track_id, captured_at,
           sum(brk) OVER (PARTITION BY camera_id, local_track_id
                          ORDER BY captured_at) AS grp
      FROM s
), t AS (
    SELECT camera_id, local_track_id, grp,
           count(*) AS n, min(captured_at) AS st, max(captured_at) AS en
      FROM g
     GROUP BY 1, 2, 3
    HAVING count(*) >= {MIN_SAMPLES}
       AND max(captured_at) - min(captured_at) >= interval '{MIN_SPAN_S} s'
)
SELECT t.camera_id, t.local_track_id, t.n, t.st, t.en, c.slug,
       (SELECT mode() WITHIN GROUP (ORDER BY e.payload->>'class_name')
          FROM events e
         WHERE e.camera_id = t.camera_id
           AND e.payload->>'track_id' = t.local_track_id::text
           AND e.at BETWEEN t.st - interval '60 s' AND t.en + interval '60 s'
           AND e.payload ? 'class_name') AS class_name
  FROM t JOIN cameras c ON c.id = t.camera_id
 WHERE NOT EXISTS (
        SELECT 1 FROM tracks_all r
         WHERE r.camera_id = t.camera_id
           AND r.local_track_id = t.local_track_id
           AND r.started_at <= t.en + interval '30 s'
           AND r.ended_at   >= t.st - interval '30 s')
 ORDER BY t.st
"""  # noqa: S608

# Class ids the detector uses, for the row's class_id column.
_CLASS_IDS = {
    "person": 0, "bicycle": 1, "car": 2, "motorcycle": 3, "bus": 5,
    "truck": 7, "cat": 15, "dog": 16,
}


def evidence_end(samples: Sequence[tuple[datetime, float]]) -> tuple[datetime, int]:
    """Where the visit really ended, and how many samples witnessed it.

    Takes (captured_at, confidence) in time order. A coasting tracker keeps
    emitting the last real detection verbatim — same confidence to the bit —
    while the subject is already out of frame, and on the gate that tail ran
    five seconds past the man leaving. Repeats are emission, not presence, so
    they end the visit where the last NEW number arrived.
    """
    last = samples[0][0]
    seen = 0
    prev: float | None = None
    for at, conf in samples:
        if prev is None or abs(conf - prev) > 1e-6:
            last = at
            seen += 1
            prev = conf
    return last, max(seen, 1)


async def _real_end(conn: asyncpg.Connection, cam: UUID, local_id: int,
                    st: datetime, en: datetime) -> tuple[datetime, int]:
    rows = await conn.fetch(
        """
        SELECT captured_at, confidence FROM track_embedding_samples
         WHERE camera_id = $1 AND local_track_id = $2 AND track_id IS NULL
           AND captured_at BETWEEN $3 AND $4
         ORDER BY captured_at
        """,
        cam, local_id, st, en,
    )
    return evidence_end([(r["captured_at"], r["confidence"]) for r in rows])


async def _recover_one(conn: asyncpg.Connection, thumbs: ThumbnailCapture,
                       row: asyncpg.Record, apply: bool) -> str:
    cam, local_id = row["camera_id"], row["local_track_id"]
    cls = row["class_name"]
    st, en = row["st"], row["en"]
    end_at, n_obs = await _real_end(conn, cam, local_id, st, en)
    span = (end_at - st).total_seconds()
    head = (f"{row['slug']}/{local_id} {st.astimezone().strftime('%d.%m %H:%M:%S %Z')} "
            f"+{span:.1f}s {cls} samples={row['n']}")
    if not apply:
        return f"would recover  {head}"

    track_id = uuid4()
    async with conn.transaction():
        await conn.execute(
            """
            INSERT INTO tracks_all (id, camera_id, local_track_id, class_id, class_name,
                                    started_at, ended_at, n_observations,
                                    ever_active, global_id, retain_until)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, true, $1,
                    $7::timestamptz + $9::interval)
            """,
            track_id, cam, local_id, _CLASS_IDS.get(cls, 0), cls, st, end_at, n_obs, ANONYMOUS,
        )
        await conn.execute(
            """
            UPDATE track_embedding_samples SET track_id = $1
             WHERE camera_id = $2 AND local_track_id = $3 AND track_id IS NULL
               AND captured_at BETWEEN $4 AND $5
            """,
            track_id, cam, local_id, st, en,
        )
        # Same SQL-side copy finalize uses: the highest-confidence sample's
        # vector becomes the track's own, without ever passing a pgvector
        # value through Python.
        await conn.execute(
            """
            UPDATE tracks_all SET embedding = (
                SELECT embedding FROM track_embedding_samples
                 WHERE track_id = $1 AND embedding IS NOT NULL
                 ORDER BY confidence DESC LIMIT 1)
             WHERE id = $1
            """,
            track_id,
        )

    seg = await conn.fetchrow(
        """
        SELECT path, started_at FROM recordings
         WHERE camera_id = $1 AND ended_at IS NOT NULL
           AND started_at <= $2 AND $2 < ended_at
         ORDER BY started_at DESC LIMIT 1
        """,
        cam, st + (end_at - st) / 2,
    )
    thumb = None
    if seg is not None:
        offset = (st + (end_at - st) / 2 - seg["started_at"]).total_seconds()
        thumb = await thumbs.from_recording(str(track_id), seg["path"], offset)
    if thumb is not None:
        await conn.execute("UPDATE tracks_all SET thumbnail_path = $1 WHERE id = $2",
                           thumb, track_id)
    return f"recovered      {head} -> {track_id}{'' if thumb else '  (no thumbnail)'}"


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--camera", help="camera slug; omit for every camera")
    ap.add_argument("--local-id", type=int, help="tracker's local id for one track")
    ap.add_argument("--since", help="ISO timestamp; only samples at or after it")
    ap.add_argument("--class", dest="klass", default="person",
                    help="class to restore, or 'any' (default: person)")
    ap.add_argument("--limit", type=int, default=0, help="stop after N tracks")
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    pool = await asyncpg.create_pool(dsn_from_env(), min_size=1, max_size=2)
    assert pool is not None
    thumbs = ThumbnailCapture(
        media_path=Path(os.environ.get("BABA_MEDIA_PATH", "/media")),
        go2rtc_url=os.environ.get("BABA_GO2RTC_URL", "http://go2rtc:1984"),
        max_width=int(os.environ.get("BABA_THUMBNAIL_MAX_WIDTH", "640")),
        go2rtc_auth=go2rtc_auth_from_env(),
    )
    try:
        async with pool.acquire() as conn:
            cam_id = None
            if args.camera:
                cam_id = await conn.fetchval("SELECT id FROM cameras WHERE slug = $1",
                                             args.camera)
                if cam_id is None:
                    raise SystemExit(f"no camera with slug {args.camera!r}")
            since = datetime.fromisoformat(args.since).astimezone(UTC) if args.since else None
            rows = await conn.fetch(_LOST_SQL, cam_id, args.local_id, since)

            wanted = [r for r in rows
                      if r["class_name"] is not None
                      and (args.klass == "any" or r["class_name"] == args.klass)]
            if args.limit:
                wanted = wanted[: args.limit]
            log.info("%d lost track(s) match; %d in the whole scan had no class evidence",
                     len(wanted), sum(1 for r in rows if r["class_name"] is None))
            for r in wanted:
                log.info("%s", await _recover_one(conn, thumbs, r, args.apply))
            if not args.apply and wanted:
                log.info("dry run — pass --apply to write")
    finally:
        await thumbs.close()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
