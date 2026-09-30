from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import time
from datetime import timedelta
from pathlib import Path
from typing import ClassVar

import asyncpg
from baba_core.observed_record import OBSERVED_MEDIA_DIRS, OBSERVED_TABLES
from baba_core.pg_listen import ResilientListener
from baba_core.retention import ANONYMOUS
from home_core.tasks import spawn

from baba_recorder.config import CameraSpec, RecorderConfig
from baba_recorder.worker import CameraRecorder

log = logging.getLogger(__name__)


def _unlink_recording_files(media_root: Path, id_paths: list) -> list:
    """Unlink each (id, rel_path) off the event loop and return the ids whose
    file is actually gone (already-missing counts as gone). A row whose file
    fails to unlink is left out, so its DB row survives rather than leaving an
    orphan file behind a deleted row. Blocking unlink on the slow/NFS media
    tier belongs in a worker thread, never on the recorder's event loop."""
    done = []
    for rid, rel in id_paths:
        full = media_root / rel
        try:
            full.unlink(missing_ok=True)
            done.append(rid)
        except Exception:
            log.exception("retention: failed to delete %s", full)
    return done


async def _load_cameras(conn: asyncpg.Connection, go2rtc_rtsp_base: str) -> list[CameraSpec]:
    rows = await conn.fetch(
        """
        SELECT id::text AS id, slug
        FROM cameras
        WHERE enabled AND recording_enabled
        """
    )
    return [
        CameraSpec(
            id=r["id"],
            slug=r["slug"],
            stream_url=f"{go2rtc_rtsp_base}/{r['slug']}",
        )
        for r in rows
    ]


# How often to close segments the indexer left open. Frequent enough that an
# orphan cannot outlive the visit it belongs to, rare enough to be invisible.
_ORPHAN_SWEEP_S = 300.0


class RecorderSupervisor:
    def __init__(self, config: RecorderConfig) -> None:
        self._config = config
        self._started = time.monotonic()
        self._workers: dict[str, CameraRecorder] = {}  # by slug
        self._pool: asyncpg.Pool | None = None
        self._listener: ResilientListener | None = None
        self._reconcile_event = asyncio.Event()
        # go2rtc names whose source was swapped under a reader already on them.
        self._source_changed: set[str] = set()
        self._stopping = asyncio.Event()
        # "Delete all recordings" — the API sets a flag + NOTIFYs; we drain it
        # off the request path. The lock serialises the NOTIFY-driven run and
        # the on-connect (restart-safety) run so they can't double-wipe.
        self._purge_event = asyncio.Event()
        self._purge_lock = asyncio.Lock()

    async def start(self) -> None:
        self._pool = await asyncpg.create_pool(self._config.dsn, min_size=1, max_size=8)
        # Close segments orphaned by a previous HARD kill (SIGKILL on a deploy
        # `compose down` that outran the 10s stop grace). A hard kill skips the
        # clean-stop final index pass, so the segment ffmpeg was writing stays
        # ended_at=NULL forever. The clip endpoint treats an open row as
        # "ongoing → overlaps everything", so a single stale orphan becomes
        # seg0 of EVERY later clip on that camera: global_in balloons to the
        # hour-old offset, the concat seek breaks, and the empty codec forces a
        # needless re-encode — the clip comes out empty and PLAY spins
        # (observed 2026-07-13 after the day's redeploys left one orphan per
        # camera).
        #
        # Spawned, NOT awaited: on a large recordings table this UPDATE can
        # take a while, and blocking start() here delays the health-marker tick
        # (which only begins once run() returns from sup.start()). That window
        # read as unhealthy and the healwatch watchdog restarted the container
        # mid-sweep — an infinite restart loop that kept recording DOWN. The
        # sweep is cleanup, never a prerequisite for recording, so it runs in
        # the background while workers start immediately.
        spawn(self._orphan_sweep_loop(), name="recorder-orphan-sweep")
        # Resilient LISTEN: reconnects after a DB blip and re-reconciles on
        # every (re)connect, so a camera added while the listener was down
        # still gets a recorder worker (the initial reconcile runs here too).
        self._listener = ResilientListener(
            self._config.dsn,
            ["cameras_changed", "go2rtc_source_changed", "recording_purge"],
            on_notify=self._on_notify,
            on_connect=self._on_listener_connect,
            name="recorder-listen",
        )
        await self._listener.start()
        spawn(self._reconcile_loop(), name="recorder-reconcile")
        spawn(self._retention_loop(), name="recorder-retention")
        spawn(self._purge_loop(), name="recorder-purge")
        log.info("recorder supervisor up: %d worker(s)", len(self._workers))

    def _on_notify(self, channel: str, payload: str) -> None:
        """Route the channels this supervisor listens on to their wake events.
        Kept tiny (runs in the listener callback) — the loops do the work."""
        if channel == "recording_purge":
            self._purge_event.set()
            return
        if channel == "go2rtc_source_changed":
            self._source_changed.add(payload)
        self._reconcile_event.set()

    async def _on_listener_connect(self) -> None:
        """Runs on every (re)connect of the LISTEN socket. Reconcile cameras as
        before, then pick up a purge that was requested while we were down — the
        NOTIFY would have been lost, but the flag persists, so a coincident
        recorder restart still finishes the wipe."""
        await self._reconcile()
        if self._pool is None:
            return
        try:
            pending = await self._pool.fetchval(
                "SELECT purge_requested_at FROM recording_settings WHERE id = 1"
            )
        except (asyncpg.UndefinedTableError, asyncpg.UndefinedColumnError):
            # Column may not exist yet on the very first boot after the api
            # applies migration 055 — never let this break the reconcile that
            # this callback also drives.
            return
        if pending is not None:
            self._purge_event.set()

    async def stop(self) -> None:
        self._stopping.set()
        log.info("recorder supervisor stopping (%d worker(s))", len(self._workers))
        if self._listener is not None:
            await self._listener.stop()
        await asyncio.gather(*(w.stop() for w in self._workers.values()))
        self._workers.clear()
        if self._pool is not None:
            await self._pool.close()

    async def _orphan_sweep_loop(self) -> None:
        """Keep closing segments the indexer left open, not just the ones it
        left open before this process started.

        This ran once, at startup, and the recorder runs for weeks. A segment
        orphaned at 05:32 was therefore still open at 08:46 — and a row with no
        `ended_at` used to answer "I cover everything from my start onwards",
        so the clip cutter picked it as the segment a window opened in and
        asked ffmpeg to seek 6457 seconds into three minutes of video. Nothing
        was written, every fallback failed the same way, and the operator got a
        spinner that never resolved, on every camera.

        The predicate is bounded now (`covers_until_sql`), so an orphan can no
        longer answer for a moment it does not hold — but leaving rows open is
        still wrong, and the repair only had to be told to keep running.
        """
        while not self._stopping.is_set():
            try:
                await self._close_orphaned_segments()
            except Exception:
                log.exception("orphan sweep failed — continuing")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=_ORPHAN_SWEEP_S
                )

    async def _close_orphaned_segments(self) -> None:
        """One-shot on startup: close every open segment that a newer segment
        already succeeds (so it is provably NOT the live one). ended_at gets
        the successor's start; codec is inherited from the camera's newest
        already-closed segment (same stream = same codec — avoids spawning an
        ffprobe per orphan). The genuinely-live segment per camera (the max
        started_at, with no successor) is left open, as it should be."""
        assert self._pool is not None
        orphans_closed = await self._pool.execute(
            """
            WITH orphan AS (
                SELECT r.id,
                    (SELECT min(r2.started_at) FROM recordings r2
                     WHERE r2.camera_id = r.camera_id AND r2.started_at > r.started_at)
                        AS next_start,
                    (SELECT r3.codec FROM recordings r3
                     WHERE r3.camera_id = r.camera_id
                       AND r3.codec IS NOT NULL AND r3.codec <> ''
                     ORDER BY r3.started_at DESC LIMIT 1) AS sib_codec
                FROM recordings r
                WHERE r.ended_at IS NULL
            )
            UPDATE recordings r
            SET ended_at   = o.next_start,
                duration_s = EXTRACT(EPOCH FROM (o.next_start - r.started_at)),
                codec      = COALESCE(NULLIF(r.codec, ''), o.sib_codec)
            FROM orphan o
            WHERE r.id = o.id AND o.next_start IS NOT NULL
            """
        )
        # Backfill codec on CLOSED segments that indexed with an empty/NULL
        # codec — this happens whenever `_probe_codec` failed for a session
        # (transient ffprobe error on the sample file), and the delta indexer
        # never revisits an old row to fix it. A blank codec is not cosmetic:
        # the clip endpoint's `reencode = any(codec not in H264)` treats blank
        # as "not H.264" and force-transcodes the whole clip (nvenc — ABSENT
        # on the Intel box — then a slow libx264), so ONE blank segment in the
        # window makes PLAY spin for seconds (observed on South/cam18: 72 blank
        # segments). Inherit the camera's dominant real codec — a stream keeps
        # one codec, so this is safe without a per-row ffprobe.
        codec_fix = await self._pool.execute(
            """
            WITH dominant AS (
                SELECT camera_id, codec
                FROM (
                    SELECT camera_id, codec,
                           row_number() OVER (
                               PARTITION BY camera_id ORDER BY count(*) DESC
                           ) AS rn
                    FROM recordings
                    WHERE codec IS NOT NULL AND codec <> ''
                    GROUP BY camera_id, codec
                ) ranked
                WHERE rn = 1
            )
            UPDATE recordings r
            SET codec = d.codec
            FROM dominant d
            WHERE r.camera_id = d.camera_id
              AND (r.codec IS NULL OR r.codec = '')
              AND r.ended_at IS NOT NULL
            """
        )
        closed = await self._pool.fetchval(
            "SELECT count(*) FROM recordings WHERE ended_at IS NULL"
        )
        log.info(
            "startup recordings reconcile: orphan close %s; codec backfill %s; "
            "%s open segment(s) remain",
            orphans_closed,
            codec_fix,
            closed,
        )

    async def _reconcile_loop(self) -> None:
        while True:
            await self._reconcile_event.wait()
            self._reconcile_event.clear()
            await asyncio.sleep(0.2)
            try:
                await self._reconcile()
            except Exception:
                log.exception("recorder reconcile failed")

    async def _reconcile(self) -> None:
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            desired = await _load_cameras(conn, self._config.go2rtc_rtsp_base)
        desired_by_slug = {c.slug: c for c in desired}
        moved, self._source_changed = self._source_changed, set()

        stale = [s for s in self._workers if s not in desired_by_slug]
        for slug in stale:
            w = self._workers.pop(slug)
            log.info("stopping recorder for %s (no longer eligible)", slug)
            await w.stop()

        for slug, spec in desired_by_slug.items():
            existing = self._workers.get(slug)
            if existing is None:
                log.info("starting recorder for %s", slug)
                w = CameraRecorder(spec, self._config, self._pool)
                w.start()
                self._workers[slug] = w
            elif existing.spec != spec or slug in moved:
                log.info("config or source changed for %s; restarting recorder", slug)
                await existing.stop()
                w = CameraRecorder(spec, self._config, self._pool)
                w.start()
                self._workers[slug] = w

    # --- retention -------------------------------------------------------
    #
    # Three layers, applied every pass and driven by the global
    # `recording_settings` singleton (re-read each pass so UI changes take
    # effect within one interval):
    #   1. AGE   — drop segments older than retention_days (all cameras).
    #   2. ACTIVITY (mode='activity') — past the buffer window, drop closed
    #      segments that overlap no detected track.
    #   3. DISK  — the safety net: while the volume is over the high-water
    #      mark, delete the oldest segments until it's back under low-water.
    #      This is what guarantees the disk never fills again.
    # File deletion runs before the DB delete so the table never references a
    # missing file; deletion is idempotent so a crash mid-pass just retries.

    _DEFAULTS: ClassVar[dict[str, object]] = {
        "mode": "continuous",
        "retention_days": 7,
        "disk_high_water_pct": 85,
        "disk_low_water_pct": 75,
        "activity_buffer_minutes": 1,
    }

    # A camera with a live track sample newer than this is treated as
    # currently active and skipped by the activity prune, so an in-flight
    # visit (whose track row only appears at finalize) isn't deleted
    # mid-visit. Must exceed the embedder's stationary sample cadence
    # (~1 sample / 5 min) so a motionless subject still reads as active.
    _ACTIVE_GRACE = timedelta(minutes=6)

    # Activity pruning only trusts `tracks` as the "had activity" signal within
    # the shortest track-retention tier. Beyond that a track that DID exist may
    # have been retention-pruned, so its absence no longer means "no activity".
    # Segments older than this are therefore left to the AGE cap alone — never
    # activity-pruned — so a `retention_days > 30` + activity-mode setup can't
    # silently delete footage the mode promised to keep.
    _TRACK_RETENTION_FLOOR = ANONYMOUS

    # Classes whose footage gets PINNED (recordings.retain_until — see
    # migration 077): segments overlapping a track of these classes live at
    # least as long as the track row itself (037's tiered deadline, extended
    # on enrollment). A person on camera is the reason the system exists;
    # parking, motion and empty frames rotate normally. A constant, not a
    # setting — "person footage survives" is an invariant, not a tunable.
    _PIN_CLASSES = (0,)  # COCO: person

    # If activity is flowing (embedder writing samples off the tracker) but the
    # finalizer (event-manager) hasn't produced a finalized track for at least
    # this long, event-manager is down/stalled — its `tracks` rows are the
    # activity prune's only signal, so pruning now would delete unfinalized
    # activity. Skip the prune until it catches up. Erring toward skip only
    # DEFERS reclaiming space; it never deletes real footage. Comfortably
    # exceeds a normal in-progress visit so short visits don't trip it.
    _FINALIZER_LAG = timedelta(minutes=15)

    async def _purge_loop(self) -> None:
        """Drains "delete all recordings" requests. Woken by the NOTIFY (fast
        path), the on-connect flag check (restart-safety) or the retention
        backstop. All three just set the event; the actual wipe happens here,
        off the API request path so no Cloudflare timeout is in play."""
        while True:
            await self._purge_event.wait()
            self._purge_event.clear()
            try:
                await self._run_purge()
            except Exception:
                log.exception("recordings purge failed")

    async def _run_purge(self) -> None:
        """Drain a pending purge, as far as its scope reaches.

        The video always goes: all segment + clip files, then the `recordings`
        rows, then the flag. Files go before rows because the indexer re-derives
        rows from files on bootstrap — a row dropped while its file survives
        just comes back. A `record` purge additionally erases everything BABA
        observed, first, so a failure there leaves the video and its index
        agreeing with each other rather than half of each gone.

        Idempotent and serialised, so a double-trigger (NOTIFY + on-connect) is
        a no-op the second time."""
        assert self._pool is not None
        async with self._purge_lock:
            pending = await self._pool.fetchrow(
                "SELECT purge_requested_at, purge_scope"
                " FROM recording_settings WHERE id = 1"
            )
            requested_at = pending["purge_requested_at"] if pending else None
            if requested_at is None:
                return  # nothing pending — a concurrent run already handled it
            whole_record = pending["purge_scope"] == "record"

            log.warning(
                "purge: wiping %s (requested %s)",
                "everything observed" if whole_record else "all media + index",
                requested_at,
            )
            media = self._config.media_path
            if whole_record:
                await self._wipe_observed_record(media)

            def _wipe() -> int:
                removed = 0
                for sub in ("segments", "clips"):
                    root = media / sub
                    if not root.is_dir():
                        continue
                    for path in root.rglob("*"):
                        try:
                            if path.is_file():
                                path.unlink()
                                removed += 1
                        except FileNotFoundError:
                            pass  # raced the retention sweep or a concurrent run
                        except OSError:
                            log.exception("purge: failed to delete %s", path)
                return removed

            n_files = await asyncio.to_thread(_wipe)

            async with self._pool.acquire() as conn, conn.transaction():
                tag = await conn.execute("DELETE FROM recordings")
                # Only clear the flag if it hasn't been re-stamped by a NEWER
                # request while we were wiping — otherwise that request would be
                # silently dropped. A newer stamp leaves the flag set so the
                # loop runs again.
                await conn.execute(
                    "UPDATE recording_settings"
                    "   SET purge_requested_at = NULL, purge_scope = NULL "
                    " WHERE id = 1 AND purge_requested_at = $1",
                    requested_at,
                )
            try:
                n_rows = int(tag.split()[-1])
            except (ValueError, IndexError):
                n_rows = 0
            log.warning(
                "recordings purge done: files=%d rows=%d", n_files, n_rows
            )

    async def _wipe_observed_record(self, media: Path) -> None:
        """Erase everything BABA has seen, keeping everything it watches with.

        One TRUNCATE over the whole set, so the foreign keys between the tables
        never have to be ordered by hand and the record is never half gone. It
        takes an exclusive lock on each, which the writers hold for the length
        of one INSERT — but a wedged transaction somewhere would otherwise put
        every camera behind this statement, so it waits ten seconds and gives
        up loudly rather than taking the pipeline down with it.

        Crops go by name, gathered before the rows that name them are dropped:
        a whole-directory sweep would race the embedder writing the next one.
        """
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            doomed: set[str] = set()
            for table, columns in (
                ("tracks", ("crop_path", "face_crop_path", "plate_crop_path",
                            "thumbnail_path")),
                ("track_embedding_samples", ("crop_path", "face_crop_path")),
                ("plate_reads", ("crop_path",)),
            ):
                for column in columns:
                    rows = await conn.fetch(
                        f"SELECT DISTINCT {column} AS p FROM {table}"  # noqa: S608
                        f" WHERE {column} IS NOT NULL"
                    )
                    doomed.update(r["p"] for r in rows)
            async with conn.transaction():
                await conn.execute("SET LOCAL lock_timeout = '10s'")
                await conn.execute(
                    f"TRUNCATE {', '.join(OBSERVED_TABLES)}"
                )

        def _unlink() -> int:
            gone = 0
            for rel in doomed:
                try:
                    (media / rel).unlink(missing_ok=True)
                    gone += 1
                except OSError:
                    log.exception("purge: failed to delete %s", rel)
            # Anything left in these directories was orphaned before today.
            for sub in OBSERVED_MEDIA_DIRS:
                root = media / sub
                if not root.is_dir():
                    continue
                for path in root.rglob("*"):
                    try:
                        if path.is_file():
                            path.unlink()
                            gone += 1
                    except FileNotFoundError:
                        pass
                    except OSError:
                        log.exception("purge: failed to delete %s", path)
            return gone

        n_files = await asyncio.to_thread(_unlink)
        log.warning(
            "purge: the observed record is gone — %d table(s), %d file(s)",
            len(OBSERVED_TABLES), n_files,
        )

    async def _retention_loop(self) -> None:
        assert self._pool is not None
        while True:
            await asyncio.sleep(self._config.retention_check_seconds)
            try:
                await self._run_retention()
            except Exception:
                log.exception("retention sweep failed")

    async def _run_retention(self) -> None:
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT mode, retention_days, disk_high_water_pct, "
                "disk_low_water_pct, activity_buffer_minutes, purge_requested_at "
                "FROM recording_settings WHERE id = 1"
            )
            s = dict(row) if row is not None else dict(self._DEFAULTS)
            # Backstop: if a purge is pending but the NOTIFY was somehow missed
            # (listener flap between heartbeat and reconnect), the retention
            # pass still catches it within one interval.
            if row is not None and row["purge_requested_at"] is not None:
                self._purge_event.set()

            # 0. PIN pass: align segment pins with the person tracks that
            # overlap them (± the activity roll, same pad the activity prune
            # honours). max() because several tracks can overlap one segment;
            # `t.retain_until > now()` because an expired track row no longer
            # defends footage — the segment and the event age out together.
            # Re-running is idempotent and also EXTENDS pins when enrollment
            # bumps a track's tier. Deliberately NOT filtered on ever_active:
            # a motionless person is a core detection requirement, and 28% of
            # person tracks on cabin read ever_active=false. A phantom's pin
            # just expires with its unnamed 30-day track row — bounded cost;
            # a lost recording of a real person is not.
            roll = timedelta(minutes=s["activity_buffer_minutes"])
            await conn.execute(
                """
                WITH keep AS (
                    SELECT r.id, max(t.retain_until) AS keep_until
                      FROM tracks t
                      JOIN recordings r
                        ON r.camera_id = t.camera_id
                       AND r.started_at < t.ended_at + $1::interval
                       AND COALESCE(r.ended_at, 'infinity'::timestamptz)
                             > t.started_at - $1::interval
                     WHERE t.class_id = ANY($2::int[])
                       AND t.retain_until > now()
                     GROUP BY r.id
                )
                UPDATE recordings r
                   SET retain_until = k.keep_until
                  FROM keep k
                 WHERE r.id = k.id
                   AND (r.retain_until IS NULL OR r.retain_until < k.keep_until)
                """,
                roll,
                list(self._PIN_CLASSES),
            )
            # 1. AGE cap (all cameras) — pinned segments are exempt until
            # their pin expires.
            n_age = await self._delete_segments(
                conn,
                "started_at < now() - $1::interval "
                "AND (retain_until IS NULL OR retain_until < now())",
                timedelta(days=s["retention_days"]),
            )
            # 2. ACTIVITY prune (only in activity mode). Activity-driven:
            # keep ended segments that overlap a finalized track (± the
            # pre/post-roll), drop the rest — EXCEPT on cameras that are
            # currently active, which we skip so a still-in-progress visit
            # (track not finalized yet) survives until its track lands and
            # the overlap test protects it.
            n_act = 0
            if s["mode"] == "activity" and await self._finalizer_healthy(conn):
                active_rows = await conn.fetch(
                    "SELECT DISTINCT camera_id FROM track_embedding_samples "
                    "WHERE captured_at > now() - $1::interval",
                    self._ACTIVE_GRACE,
                )
                active_ids = [r["camera_id"] for r in active_rows]
                n_act = await self._delete_segments(
                    conn,
                    "ended_at IS NOT NULL "
                    "AND ended_at > now() - $3::interval "
                    "AND (retain_until IS NULL OR retain_until < now()) "
                    "AND camera_id <> ALL($2::uuid[]) "
                    "AND NOT EXISTS (SELECT 1 FROM tracks t "
                    "  WHERE t.camera_id = recordings.camera_id "
                    "  AND t.started_at < recordings.ended_at + $1::interval "
                    "  AND t.ended_at   > recordings.started_at - $1::interval)",
                    roll,
                    active_ids,
                    self._TRACK_RETENTION_FLOOR,
                )
            # 3. DISK safety net.
            n_disk = await self._disk_prune(conn, s["disk_high_water_pct"], s["disk_low_water_pct"])
            if n_age or n_act or n_disk:
                log.info(
                    "retention: removed age=%d activity=%d disk=%d (mode=%s, keep=%dd)",
                    n_age,
                    n_act,
                    n_disk,
                    s["mode"],
                    s["retention_days"],
                )

    async def _finalizer_healthy(self, conn) -> bool:
        """False when the activity prune must be skipped because event-manager
        (the finalizer that writes `tracks` rows) is down/stalled while activity
        is still happening.

        Signal: the embedder writes `track_embedding_samples` straight off the
        tracker, independent of event-manager. So "recent samples but stale
        newest finalized track" means activity is flowing yet nothing is being
        finalized — event-manager is the missing link. The whole test runs in
        SQL against the DB clock so there's no host-clock/timezone skew."""
        row = await conn.fetchrow(
            """
            SELECT
              (SELECT max(captured_at) FROM track_embedding_samples)
                  > now() - $1::interval                       AS recent_activity,
              COALESCE(
                (SELECT max(captured_at) FROM track_embedding_samples)
                  - (SELECT max(ended_at) FROM tracks) > $2::interval,
                (SELECT max(captured_at) FROM track_embedding_samples) IS NOT NULL
              )                                                 AS finalize_lagging
            """,
            self._ACTIVE_GRACE,
            self._FINALIZER_LAG,
        )
        stalled = bool(row["recent_activity"]) and bool(row["finalize_lagging"])
        if stalled:
            log.warning(
                "activity prune SKIPPED this cycle: recent activity but the "
                "finalizer (event-manager) is stalled — not deleting "
                "potentially-unfinalized footage until it catches up."
            )
        return not stalled

    async def recording_health(self) -> tuple[bool, list[str]]:
        """Per-camera segment freshness for the health marker.

        Returns (flowing, stale_slugs). `stale_slugs` names every enabled
        camera whose newest segment is older than the freshness window, so a
        single dead stream is loud without touching the others. `flowing` is
        False only when there ARE enabled cameras but NONE has a fresh segment
        — the recorder as a whole is stuck (go2rtc down, DB wedged), which is
        restart-worthy; one dead camera keeps the service healthy so its peers
        go on recording."""
        if self._pool is None:
            return True, []
        stale_after = max(2 * self._config.segment_seconds, 120) + self._config.poll_seconds
        # Right after a restart the newest segment still dates from before it,
        # and a deploy's build and database upgrade can outlast the window, so
        # no camera is judged before this process has had the window to write.
        if time.monotonic() - self._started < stale_after:
            return True, []
        rows = await self._pool.fetch(
            """
            SELECT c.slug,
                   EXTRACT(EPOCH FROM (now() - max(r.started_at))) AS age
              FROM cameras c
              LEFT JOIN recordings r ON r.camera_id = c.id
             WHERE c.enabled AND c.recording_enabled
             GROUP BY c.slug
            """
        )
        if not rows:
            return True, []  # nothing to record — vacuously healthy
        stale = [r["slug"] for r in rows if r["age"] is None or r["age"] > stale_after]
        flowing = len(stale) < len(rows)
        return flowing, stale

    async def _delete_segments(self, conn, where: str, *args) -> int:
        """Delete recordings matching `where` (file first, then row). The
        WHERE may reference the `recordings` table by name (for sub-selects)."""
        rows = await conn.fetch(f"SELECT id, path FROM recordings WHERE {where}", *args)  # noqa: S608
        if not rows:
            return 0
        id_paths = [(r["id"], r["path"]) for r in rows]
        deleted = await asyncio.to_thread(
            _unlink_recording_files, self._config.media_path, id_paths
        )
        if deleted:
            await conn.execute(
                "DELETE FROM recordings WHERE id = ANY($1::uuid[])", deleted
            )
        return len(deleted)

    async def _disk_prune(self, conn, hi_pct: int, lo_pct: int) -> int:
        """While the media volume is over the high-water mark, delete the
        oldest CLOSED segments (any camera) until it's back under low-water.

        CRITICAL safety: `shutil.disk_usage` measures the WHOLE filesystem, not
        BABA's own footprint. The media tier is frequently an NFS export or a
        pool shared with other tenants. If an unrelated tenant fills that pool,
        deleting BABA recordings barely moves `free` — so a naive loop would
        keep deleting until it wiped the ENTIRE recording history chasing free
        space it can never reclaim. Guard with a circuit breaker: after each
        delete batch, if `free` did not actually increase, the pressure is NOT
        BABA's prunable media → stop and raise a loud ALARM instead of
        self-destructing. (Fail loud, never silently degrade.)"""
        loop = asyncio.get_running_loop()

        def usage():
            return shutil.disk_usage(self._config.media_path)

        du = await loop.run_in_executor(None, usage)
        if du.total == 0 or (du.used / du.total) * 100.0 <= hi_pct:
            return 0
        target_free = du.total * (1.0 - lo_pct / 100.0)
        removed = 0
        external_pressure = False
        while True:
            du_before = await loop.run_in_executor(None, usage)
            if du_before.free >= target_free:
                break
            batch = await conn.fetch(
                "SELECT id, path FROM recordings WHERE ended_at IS NOT NULL "
                "AND (retain_until IS NULL OR retain_until < now()) "
                "ORDER BY started_at ASC LIMIT 100"
            )
            if not batch:
                # Everything prunable is gone. If pinned person footage is
                # what's left, the disk sweep does NOT eat it — a person
                # segment surviving is the invariant this column exists for.
                # That makes the disk genuinely full, which must surface as
                # an incident, not be silently traded for the archive.
                pinned = await conn.fetchval(
                    "SELECT count(*) FROM recordings "
                    "WHERE ended_at IS NOT NULL AND retain_until >= now()"
                )
                if pinned:
                    log.error(
                        "disk-prune ALARM: %s is over %d%% and only %d PINNED "
                        "person segment(s) remain — refusing to delete them. "
                        "The media volume is undersized for the pinned "
                        "history; grow the disk or archive/export the pinned "
                        "footage.",
                        self._config.media_path,
                        hi_pct,
                        pinned,
                    )
                break
            id_paths = [(r["id"], r["path"]) for r in batch]
            deleted = await asyncio.to_thread(
                _unlink_recording_files, self._config.media_path, id_paths
            )
            if deleted:
                await conn.execute(
                    "DELETE FROM recordings WHERE id = ANY($1::uuid[])", deleted
                )
            removed += len(deleted)
            # Circuit breaker: deleting a batch of closed segments frees real
            # bytes. If `free` didn't rise afterwards, an external tenant is
            # consuming the shared volume at least as fast as we prune — keeping
            # going would destroy BABA's whole history without ever reaching
            # target. Stop and alarm.
            du_after = await loop.run_in_executor(None, usage)
            if du_after.free <= du_before.free:
                external_pressure = True
                break
        if external_pressure:
            log.error(
                "disk-prune ALARM: %s is over %d%% but deleting %d BABA segment(s) "
                "did not free space — the pressure is NOT BABA recordings (a co-tenant "
                "is filling the shared/NFS volume). Stopped to avoid wiping the recording "
                "history; free the shared volume or give BABA a dedicated media disk.",
                self._config.media_path,
                hi_pct,
                removed,
            )
        elif removed:
            log.warning(
                "disk-prune: volume over %d%%, deleted %d oldest segment(s)", hi_pct, removed
            )
        return removed
