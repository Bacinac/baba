"""Per-camera recording worker. Spawns one ffmpeg subprocess and watches its
output directory for new closed segments to index in the DB."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
from baba_core import mask_credentials
from baba_core.paths import MediaLayout
from home_core.tasks import spawn

from baba_recorder.config import CameraSpec, RecorderConfig

log = logging.getLogger(__name__)

# Match the strftime pattern we hand to ffmpeg: 2026-05-14_14-30-00.mp4
_SEGMENT_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})\.mp4$")
# Wall-clock stamping (see the ffmpeg command) gives frames that arrive in one
# TCP read the same tick; the muxer moves each one a tick past the last and says
# so, and the next frame of the burst is then behind the tick the muxer made up.
# That is the design working. A timestamp that runs back past the burst is not.
_NON_MONOTONIC_DTS_RE = re.compile(
    r"Non-monotonic DTS .*previous: (\d+), current: (\d+); changing to (\d+)"
)
# Probing reads the first frames of a session in one burst, so the next frame's
# arrival looks like a jump of a thousand frame times. It says nothing about
# the camera.
_PROBE_DISCONTINUITY_RE = re.compile(r"DTS discontinuity in stream \d+: packet \d+ with DTS")


def _parse_segment_started_at(name: str) -> datetime | None:
    m = _SEGMENT_FILE_RE.match(name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d_%H-%M-%S").replace(tzinfo=UTC)
    except ValueError:
        return None


def _scan_segment_dir(out_dir: Path) -> list[tuple[Path, datetime, float, int | None]]:
    """Blocking directory scan — glob + one stat() per segment file — returning
    (path, started_at, mtime, size) sorted by name. Pure sync so it runs inside
    asyncio.to_thread(): on the slow media tier a camera dir holds thousands of
    files and this must NOT execute on the event-loop thread (see _index_once).
    Files with an unparseable name or that vanish mid-scan are skipped."""
    out: list[tuple[Path, datetime, float, int | None]] = []
    for f in sorted(out_dir.glob("*.mp4")):
        ts = _parse_segment_started_at(f.name)
        if ts is None:
            continue
        try:
            st = f.stat()
        except FileNotFoundError:
            continue
        out.append((f, ts, st.st_mtime, st.st_size))
    return out


class CameraRecorder:
    def __init__(self, spec: CameraSpec, config: RecorderConfig, pool: asyncpg.Pool) -> None:
        self._spec = spec
        self._config = config
        self._pool = pool
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        # Cached video codec name (e.g. "h264", "hevc") for this camera's
        # current ffmpeg session. Set lazily by probing the first closed
        # segment via ffprobe — we record `-c copy` so the codec mirrors
        # whatever the RTSP source advertises, which varies per vendor.
        # Reset on each new _record_session so a camera config swap (e.g.
        # the camera switched to its substream) re-probes.
        self._probed_codec: str | None = None
        # What that same probe saw of the picture.
        self._probed_size: tuple[int, int] | None = None
        # When the current ffmpeg started, so a probe can tell this session's
        # output from what retention happens to have kept.
        self._session_started_at: datetime | None = None
        # Checkpoint for the index pass. Originally `_index_once` UPSERT-ed
        # every segment in the output dir on every 5s poll, which on a long-
        # running install scales to 5 cameras × 600+ files × 12 polls/min
        # = thousands of no-op UPSERTs/minute hitting Postgres. Now we only
        # touch files whose mtime moved past this checkpoint, PLUS the two
        # most recent rows on every pass to keep `ended_at`/size fresh for
        # the open segment and the segment that just closed before it. The
        # cost stays O(1) regardless of how many days of recordings exist.
        self._last_indexed_mtime: float = 0.0

    @property
    def spec(self) -> CameraSpec:
        return self._spec

    def start(self) -> None:
        if self._task is not None:
            return
        self._task = spawn(self._run(), name=f"recorder-{self._spec.slug}", log=log)

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=15)
            except TimeoutError:
                log.warning("recorder %s did not stop in 15s; cancelling", self._spec.slug)
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._task
            self._task = None

    async def _run(self) -> None:
        out_dir = MediaLayout(self._config.media_path).camera_segments(self._spec.slug)
        out_dir.mkdir(parents=True, exist_ok=True)
        log.info(
            "recorder starting: camera=%s rtsp=%s segment=%ds out=%s",
            self._spec.slug,
            mask_credentials(self._spec.stream_url),
            self._config.segment_seconds,
            out_dir,
        )

        backoff = 1.0
        while not self._stop.is_set():
            try:
                await self._record_session(out_dir)
                # ffmpeg exited cleanly — unusual but treat as restart.
                log.info("ffmpeg exited cleanly for %s, restarting", self._spec.slug)
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("recorder session crashed for %s", self._spec.slug)
            if self._stop.is_set():
                break
            sleep_for = min(backoff, 30.0)
            log.info("recorder %s sleeping %.1fs before reconnect", self._spec.slug, sleep_for)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=sleep_for)
            backoff *= 2

        log.info("recorder stopped: camera=%s", self._spec.slug)

    async def _probe_codec(self, sample_file: Path) -> str | None:
        """Run ffprobe on a closed segment and return the video codec name.
        Returns None on failure — caller falls back to NULL in DB so a
        later pass can fix it. Caches the result for the rest of the
        session via self._probed_codec."""
        if self._probed_codec is not None:
            return self._probed_codec
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            # Dimensions ride along at no cost — it is the same call.
            "-show_entries",
            "stream=codec_name,width,height",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(sample_file),
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
            finally:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
        except (TimeoutError, FileNotFoundError, OSError) as e:
            log.warning("recorder %s: ffprobe failed: %s", self._spec.slug, e)
            return None
        if proc.returncode != 0:
            log.warning(
                "recorder %s: ffprobe exit %d: %s",
                self._spec.slug,
                proc.returncode,
                stderr.decode(errors="replace").strip(),
            )
            return None
        fields = stdout.decode(errors="replace").split()
        codec = fields[0].strip().lower() if fields else None
        try:
            self._probed_size = (int(fields[1]), int(fields[2]))
        except (IndexError, ValueError):
            self._probed_size = None
        if codec:
            log.info(
                "recorder %s: probed codec=%s size=%s", self._spec.slug, codec,
                "x".join(map(str, self._probed_size)) if self._probed_size else "?",
            )
            self._probed_codec = codec
        return codec

    async def _note_size(self) -> None:
        """Write down the picture this camera's stream carries; the plate
        reader maps its zones onto recordings by it."""
        if self._probed_size is None or self._pool is None:
            return
        width, height = self._probed_size
        await self._pool.execute(
            """
            UPDATE cameras SET stream_width = $2, stream_height = $3
             WHERE id = $1::uuid
               AND (stream_width IS DISTINCT FROM $2 OR stream_height IS DISTINCT FROM $3)
            """,
            self._spec.id, width, height,
        )

    async def _record_session(self, out_dir: Path) -> None:
        # ffmpeg writes one file per segment; strftime puts the start time in
        # the filename so we can both index by name and offer it as a useful
        # default sort. -c copy means no transcode — keep source bytes.
        # Reset codec cache: the camera config may have changed between
        # sessions (operator edited stream_url), so don't carry a stale value.
        self._probed_codec = None
        self._probed_size = None
        self._session_started_at = datetime.now(UTC)
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-rtsp_transport",
            "tcp",
            # Socket I/O timeout: a stalled stream makes ffmpeg exit (the run
            # loop then reconnects) instead of blocking on a read forever. The
            # rtsp demuxer's own option is `-timeout` (µs); `-rw_timeout` is a
            # generic AVIO option the rtsp demuxer rejects outright ("Option
            # rw_timeout not found"), so it must not be used for an rtsp input.
            "-timeout",
            str(self._config.rtsp_timeout_us),
            # Stamp frames with the server clock at receipt instead of the
            # camera's RTSP timestamps. Some cameras stamp garbage: with
            # `-c copy` the lie is copied verbatim and the segment's moov
            # declares a fraction of its real timeline (measured on cabin:
            # 65 MB / 5 min of media declaring 0.36 s — every seek beyond
            # that cuts nothing). Arrival time over TCP tracks the frame
            # rate closely, and the recording timeline should be the
            # server's wall clock anyway — it is what segments are indexed
            # and windowed by.
            "-use_wallclock_as_timestamps",
            "1",
            # Nothing here decodes but the probe, and on a stream that is
            # already running it lands mid-GOP: every frame before the next
            # keyframe is a decode error about a picture it never had.
            "-skip_frame",
            "nokey",
            "-i",
            self._spec.stream_url,
            "-c",
            "copy",
            "-an",  # drop audio for now
            # The parser can hand a session's first frame over with a DTS and
            # no PTS, which the muxer warns "will stop working". The wall clock
            # stamps every frame pts = dts anyway.
            "-bsf:v",
            r"setts=pts=if(eq(PTS\,NOPTS)\,DTS\,PTS):dts=DTS",
            "-f",
            "segment",
            "-segment_time",
            str(self._config.segment_seconds),
            # Cut on wall-clock boundaries (e.g. 00:00, 00:05, 00:10) rather
            # than X seconds after start. The first segment is short by
            # design — it ends at the next clock boundary; every subsequent
            # one is full-length.
            "-segment_atclocktime",
            "1",
            "-segment_format",
            "mp4",
            "-segment_format_options",
            "movflags=+faststart+empty_moov",
            "-reset_timestamps",
            "1",
            "-strftime",
            "1",
            str(out_dir / "%Y-%m-%d_%H-%M-%S.mp4"),
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        # Background tasks while ffmpeg runs: an indexer that scans the
        # output dir periodically and a stderr forwarder for diagnostics.
        indexer = spawn(self._index_loop(out_dir), name=f"index-{self._spec.slug}", log=log)
        stderr_fwd = spawn(self._forward_stderr(proc), name=f"stderr-{self._spec.slug}", log=log)

        try:
            # Wait for either ffmpeg to exit or stop to be requested.
            wait_proc = spawn(proc.wait(), name=f"ffmpeg-exit-{self._spec.slug}", log=log)
            wait_stop = spawn(self._stop.wait(), name=f"stop-{self._spec.slug}", log=log)
            done, _ = await asyncio.wait(
                {wait_proc, wait_stop}, return_when=asyncio.FIRST_COMPLETED
            )
            if wait_stop in done:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=10)
                except TimeoutError:
                    proc.kill()
                    await proc.wait()
            for t in (wait_proc, wait_stop):
                if not t.done():
                    t.cancel()
        finally:
            indexer.cancel()
            stderr_fwd.cancel()
            for t in (indexer, stderr_fwd):
                with contextlib.suppress(asyncio.CancelledError):
                    await t

            # One final index pass so the last closed segment lands in DB.
            await self._index_once(out_dir, also_close_pending=True)

    async def _forward_stderr(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr is not None
        burst: tuple[int, int] | None = None
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            text = line.decode(errors="replace").rstrip()
            if not text:
                continue
            level = logging.WARNING
            if m := _NON_MONOTONIC_DTS_RE.search(text):
                previous, current, assigned = map(int, m.groups())
                if burst and previous == burst[1] and current >= burst[0]:
                    burst = (burst[0], assigned)
                    level = logging.DEBUG
                elif previous == current:
                    burst = (current, assigned)
                    level = logging.DEBUG
                else:
                    burst = None
            elif _PROBE_DISCONTINUITY_RE.search(text):
                level = logging.DEBUG
            log.log(level, "[ffmpeg %s] %s", self._spec.slug, text)

    async def _index_loop(self, out_dir: Path) -> None:
        try:
            while True:
                await asyncio.sleep(self._config.poll_seconds)
                try:
                    await self._index_once(out_dir, also_close_pending=False)
                except Exception:
                    log.exception("index pass failed for %s", self._spec.slug)
        except asyncio.CancelledError:
            return

    async def _index_once(self, out_dir: Path, *, also_close_pending: bool) -> None:
        """Scan output dir; upsert a row per segment file. The currently-open
        segment is indexed with ended_at=NULL so events that happen during it
        can still link to a recording. When a newer file appears next pass,
        we set the previous one's ended_at.
        On the final-finalize pass (`also_close_pending`), close out the last
        file too using its mtime.

        After the bootstrap pass that catches up history on cold start, only
        files whose mtime moved past `_last_indexed_mtime` are re-processed.
        The two most recent files are ALWAYS included because (a) the open
        segment's size/codec needs refreshing as ffmpeg writes and (b) the
        previous segment's `ended_at` gets set the first time we notice its
        successor. Without this, every poll re-UPSERT-ed every file in the
        dir — see __init__ for the cost figures."""
        # The directory scan (glob + a stat() per file) is SYNCHRONOUS and, on
        # the slow media tier with thousands of segments per camera, takes long
        # enough to freeze the event loop — which starves the health-marker
        # tick (→ the healwatch watchdog restarts the container → bootstrap
        # re-scans everything → hangs again, forever) and stalls indexing on
        # every OTHER camera too. Run the whole scan off-thread so the loop
        # keeps breathing. Size is collected here as well so the UPSERT loop
        # below never stat()s on the loop thread. (Root cause of the 2026-07-14
        # indexer stall: patio alone had 2663 files.)
        scan = await asyncio.to_thread(_scan_segment_dir, out_dir)
        if not scan:
            return
        starts: list[tuple[Path, datetime]] = [(p, ts) for p, ts, _mt, _sz in scan]
        starts_mtime: list[float] = [mt for _p, _ts, mt, _sz in scan]
        starts_size: list[int | None] = [sz for _p, _ts, _mt, sz in scan]

        last_idx = len(starts) - 1
        # Probe from the newest closed segment THIS SESSION wrote. The open one
        # (last index) may have a torn moov atom and ffprobe can hang on it, so
        # it is skipped — but the oldest in the directory, which this used to
        # take, is whatever survives retention: on west that was a file from two
        # days earlier. Every session re-read it, so the codec written onto 730
        # rows was the old file's while the segments were the substream's, and
        # the size written down was a stream that had already ended.
        if self._probed_codec is None:
            mine = [
                path for path, ts in starts[:last_idx]
                if self._session_started_at and ts >= self._session_started_at
            ]
            if mine:
                await self._probe_codec(mine[-1])
                await self._note_size()

        # Decide which file indices to actually UPSERT this pass. Bootstrap
        # (first-ever pass since process start) and shutdown both want a
        # full sweep so we don't drop rows on the floor. Steady state runs
        # process only the delta plus the trailing two.
        if self._last_indexed_mtime == 0.0 or also_close_pending:
            indices_to_process: list[int] = list(range(len(starts)))
        else:
            checkpoint = self._last_indexed_mtime
            # Strict >: equality is "we already saw this exact mtime"; if
            # ffmpeg rewrote the file with identical mtime (unusual but
            # possible) the trailing-two fallback still covers it for the
            # open segment.
            delta = {i for i, mt in enumerate(starts_mtime) if mt > checkpoint}
            # Always include the trailing two: the open segment (last_idx)
            # and the one that closed just before it (last_idx - 1). The
            # second-to-last needs its `ended_at` populated the first time
            # we observe its successor.
            always = {max(0, last_idx - 1), last_idx}
            indices_to_process = sorted(delta | always)

        for i in indices_to_process:
            path, started_at = starts[i]
            is_open = i == last_idx and not also_close_pending
            if is_open:
                # ffmpeg is still writing — leave ended_at/duration/size
                # untouched on conflict, but insert with NULLs for new rows.
                ended_at = None
                duration_s: float | None = None
                size_bytes: int | None = None
            else:
                # A closed segment's true end is its file mtime (when ffmpeg
                # finished writing it). The next segment's start only matches
                # that when segments are contiguous — across a stream loss /
                # ffmpeg reconnect there's a real gap, and using next_start
                # there inflates ended_at/duration to span the outage. That
                # then misroutes an event that happened during the outage to
                # this segment and pushes the thumbnail seek past the file's
                # real length. So cap ended_at by the file mtime. mtime/size
                # come from the off-thread scan above — never stat() here (loop
                # thread).
                mtime = datetime.fromtimestamp(starts_mtime[i], tz=UTC)
                if i + 1 < len(starts):
                    ended_at = starts[i + 1][1]
                    if mtime < ended_at:
                        ended_at = mtime
                else:
                    ended_at = mtime
                duration_s = (ended_at - started_at).total_seconds()
                size_bytes = starts_size[i]

            rel_path = MediaLayout.rel_camera_segments(self._spec.slug) + path.name
            # COALESCE so we don't overwrite known values with NULLs from
            # the in-flight pass that follows a closing pass — same logic
            # applies to codec: a later probe-successful pass replaces
            # NULL with the real codec without re-touching earlier finalised
            # metadata.
            codec = self._probed_codec  # may be None until first probe
            try:
                await self._pool.execute(
                    """
                    INSERT INTO recordings
                        (camera_id, started_at, ended_at, duration_s, path, size_bytes, codec)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    ON CONFLICT (path) DO UPDATE
                    SET ended_at   = COALESCE(EXCLUDED.ended_at,   recordings.ended_at),
                        duration_s = COALESCE(EXCLUDED.duration_s, recordings.duration_s),
                        size_bytes = COALESCE(EXCLUDED.size_bytes, recordings.size_bytes),
                        codec      = COALESCE(EXCLUDED.codec,      recordings.codec)
                    """,
                    self._spec.id,
                    started_at,
                    ended_at,
                    duration_s,
                    rel_path,
                    size_bytes,
                    codec,
                )
            except asyncpg.exceptions.ForeignKeyViolationError:
                # Camera was deleted from the DB while we were finalizing its
                # last segment. The cascade already removed any existing rows
                # for this camera, so there is nothing left to index.
                log.debug(
                    "recorder %s: camera deleted during finalize, skipping segment index",
                    self._spec.slug,
                )
                return

        # Bump the checkpoint *after* a successful pass so a mid-pass crash
        # doesn't leave us pretending we caught files we never UPSERT-ed.
        # max() across all observed mtimes — not just the processed subset
        # — because if we hit only the trailing two but there are older
        # files with bigger mtimes (shouldn't happen normally, but safe),
        # we still advance.
        if starts_mtime:
            highest = max(starts_mtime)
            if highest > self._last_indexed_mtime:
                self._last_indexed_mtime = highest
