"""DB-owned retention sweeper for tracks, their media files, and what else a
sighting leaves behind.

Walks `tracks_all` rows whose `retain_until` deadline has passed, unlinks
the per-track jpegs (crop, thumbnail) and per-sample jpegs (crop +
face crop), then deletes the rows whose files are gone. Media
and DB stay coupled: the operator can't end up with broken thumbnail
references because a media-side cleanup ran without telling the DB.

Tier defaults (set by event-manager on finalize and bumped by
reference-photo / label enrollment in routes_identities):

  - 30 days  for unnamed (auto-detected) tracks
  - 60 days  for labeled identities without reference photos
  - 90 days  for labeled identities WITH reference photos

What else a sighting leaves behind follows the same tiers, counted from when
it ended: a plate read and its crop, a closed presence episode, a released
parking place. An open episode or place is the present, never history. A plate
read stays while a track or a parking place still cites it, since that is where
its crop is shown.

Disabled with BABA_TRACKS_RETENTION_SWEEP_INTERVAL_S=0 if operator
wants pruning gated to manual scripts.

Lives in api (not event-manager) because it's I/O-bound on the media
mount and benefits from the api's existing pool + media_path config.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path

import asyncpg
from baba_core.native import run_native
from baba_core.retention import ANONYMOUS, tier_sql
from baba_core.task_owner import finish_on_cancel

log = logging.getLogger(__name__)


# Batch size per pass — keeps each iteration's transaction bounded
# so a backlog of expired rows doesn't hold a long-running tx that
# blocks vacuum / autovacuum. The outer loop drains the backlog by
# running back-to-back batches until a pass returns zero.
_DEFAULT_BATCH = 200

# Hourly is the right default: deadlines have day-level precision so
# anything more frequent is wasted CPU; longer intervals risk holding
# pruned-but-still-DB-referenced jpegs for too long after the
# operator visibly deleted the identity (UX expectation: "I deleted
# it, the files should be gone by tonight").
_DEFAULT_INTERVAL_S = 3600


def _unlink_row_media(media_root: Path, row_paths: dict) -> list:
    """Unlink every media file for each claimed row, in a worker thread.
    Returns the ids of rows whose files ALL came off disk (a file already
    gone counts as done); a row with any real unlink failure is left out so
    its row survives to be retried, never orphaning the file behind a deleted
    row. Blocking stat/unlink on the slow HDD media tier — hundreds of syscalls
    per batch — so this runs off the event loop. The caller owns each row
    until its files and metadata have been removed."""
    deletable = []
    for rid, paths in row_paths.items():
        ok = True
        for rel in paths:
            abs_path = media_root / rel
            try:
                abs_path.unlink(missing_ok=True)
            except OSError as e:
                ok = False
                log.warning("retention sweeper: failed to unlink %s (%s)", abs_path, e)
        if ok:
            deletable.append(rid)
    return deletable


async def _prune_one_batch(
    pool: asyncpg.Pool,
    media_root: Path,
    batch_size: int,
) -> int:
    """One batch: read N expired rows, unlink their media off the event loop,
    then delete only the rows whose files are actually gone. Returns the number
    of rows deleted so the outer loop knows whether to keep draining."""
    return await finish_on_cancel(
        _prune_track_transaction(pool, media_root, batch_size), name="track-retention-batch", log=log,
    )


async def _prune_track_transaction(pool, media_root: Path, batch_size: int) -> int:
    async with pool.acquire() as conn, conn.transaction():
        return await _prune_locked_tracks(conn, media_root, batch_size)


async def _prune_locked_tracks(conn, media_root: Path, batch_size: int) -> int:
    rows = await conn.fetch(
        """
        SELECT t.id, t.crop_path, t.thumbnail_path, t.face_crop_path,
               (SELECT array_agg(s.crop_path) FROM track_embedding_samples s
                  WHERE s.track_id = t.id AND s.crop_path IS NOT NULL) AS sample_crops,
               (SELECT array_agg(s.face_crop_path) FROM track_embedding_samples s
                  WHERE s.track_id = t.id AND s.face_crop_path IS NOT NULL) AS sample_face_crops
        FROM tracks_all t
        WHERE t.retain_until IS NOT NULL
          AND t.retain_until < now()
        ORDER BY t.retain_until ASC
        LIMIT $1
        FOR UPDATE OF t SKIP LOCKED
        """,
        batch_size,
    )
    if not rows:
        return 0

    row_paths: dict = {}
    for r in rows:
        paths: list[str] = []
        if r["crop_path"]:
            paths.append(r["crop_path"])
        if r["thumbnail_path"]:
            paths.append(r["thumbnail_path"])
        if r["face_crop_path"]:
            paths.append(r["face_crop_path"])
        if r["sample_crops"]:
            paths.extend(r["sample_crops"])
        if r["sample_face_crops"]:
            paths.extend(r["sample_face_crops"])
        row_paths[r["id"]] = paths

    deletable = await run_native(_unlink_row_media, media_root, row_paths)
    if not deletable:
        return 0
    await conn.execute(
        "DELETE FROM tracks_all WHERE id = ANY($1::uuid[])",
        deletable,
    )
    return len(deletable)


async def _prune_closed(pool: asyncpg.Pool, table: str, ended: str, batch_size: int) -> int:
    """One batch of a record that carries no media: rows closed longer ago than
    their identity's tier. Returns the number deleted."""
    status = await pool.execute(
        f"""
        DELETE FROM {table} WHERE id IN (
            SELECT e.id FROM {table} e
            WHERE e.{ended} < now() - $2::interval
              AND e.{ended} + {tier_sql("e.global_id")} < now()
            ORDER BY e.{ended}
            LIMIT $1
        )
        """,  # noqa: S608
        batch_size, ANONYMOUS,
    )
    return int(status.split()[-1])


async def _prune_plate_reads(pool: asyncpg.Pool, media_root: Path, batch_size: int) -> int:
    """One batch of expired plate reads: crop off disk first, then only the rows
    whose crop is gone."""
    return await finish_on_cancel(
        _prune_plate_transaction(pool, media_root, batch_size), name="plate-retention-batch", log=log,
    )


async def _prune_plate_transaction(pool, media_root: Path, batch_size: int) -> int:
    async with pool.acquire() as conn, conn.transaction():
        return await _prune_locked_plate_reads(conn, media_root, batch_size)


async def _prune_locked_plate_reads(conn, media_root: Path, batch_size: int) -> int:
    rows = await conn.fetch(
        f"""
        SELECT r.id, r.crop_path FROM plate_reads r
        WHERE r.read_at < now() - $2::interval
          AND r.read_at + {tier_sql("r.global_id")} < now()
          AND r.track_id IS NULL
          AND NOT EXISTS (SELECT 1 FROM place_occupancy o WHERE o.plate_read_id = r.id)
        ORDER BY r.read_at
        LIMIT $1
        FOR UPDATE OF r SKIP LOCKED
        """,  # noqa: S608
        batch_size, ANONYMOUS,
    )
    if not rows:
        return 0
    row_paths = {r["id"]: [r["crop_path"]] if r["crop_path"] else [] for r in rows}
    deletable = await run_native(_unlink_row_media, media_root, row_paths)
    if deletable:
        await conn.execute("DELETE FROM plate_reads WHERE id = ANY($1::uuid[])", deletable)
    return len(deletable)


async def prune_expired(pool: asyncpg.Pool, media_root: Path, batch_size: int) -> dict[str, int]:
    """Drain every expired record, each in batches until a pass comes back
    short. Tracks go first because a track going frees its plate read, and
    parking places before plate reads for the same reason. One kind failing
    leaves the others to run."""
    kinds = (
        ("tracks", lambda: _prune_one_batch(pool, media_root, batch_size)),
        ("parking places", lambda: _prune_closed(pool, "place_occupancy", "released_at", batch_size)),
        ("presence episodes", lambda: _prune_closed(pool, "presence_episodes", "departed_at", batch_size)),
        ("plate reads", lambda: _prune_plate_reads(pool, media_root, batch_size)),
    )
    pruned: dict[str, int] = {}
    for kind, batch in kinds:
        total = 0
        try:
            while True:
                n = await batch()
                total += n
                if n < batch_size:
                    break
        except Exception:
            log.exception("retention sweeper: %s failed, will retry next tick", kind)
        if total:
            pruned[kind] = total
    return pruned


async def run_sweeper(
    pool: asyncpg.Pool,
    media_root: Path,
    stop: asyncio.Event,
) -> None:
    """Long-running background task. Sleeps between passes, drains
    each pass in batches so a backlog clears within one tick."""
    interval_s = int(
        os.environ.get(
            "BABA_TRACKS_RETENTION_SWEEP_INTERVAL_S",
            _DEFAULT_INTERVAL_S,
        ),
    )
    batch_size = int(
        os.environ.get(
            "BABA_TRACKS_RETENTION_SWEEP_BATCH",
            _DEFAULT_BATCH,
        ),
    )
    if interval_s <= 0:
        log.info("tracks retention sweeper disabled (BABA_TRACKS_RETENTION_SWEEP_INTERVAL_S=0)")
        return

    # Small startup delay so the sweeper doesn't fire right when the
    # api is still warming up its model loads + go2rtc sync. 60s is
    # plenty to let the cold start settle.
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=min(60, interval_s))

    while not stop.is_set():
        pruned = await prune_expired(pool, media_root, batch_size)
        if pruned:
            log.info(
                "retention sweeper: pruned %s",
                ", ".join(f"{n} {kind}" for kind, n in pruned.items()),
            )
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
