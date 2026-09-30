"""Mirror the `cameras` table into go2rtc's stream registry.

Why: go2rtc loads its initial config from yaml, but adding a camera through
our UI needs to make it immediately viewable. Instead of regenerating yaml
+ restarting go2rtc, we use its REST API (PUT/DELETE /api/streams) to add
and remove streams live.

Per camera (= slug) we register what it exposes: "{slug}" for its
stream_url, which the recorder records, and "{slug}_sub" for its substream
when it has one. The ingestor reads whichever `cameras.analysis_stream` names.

`/api/frame.jpeg` (snapshots for grid thumbnails) extracts a frame on demand
— no continuous MJPEG pipeline required. If snapshot latency becomes a
bottleneck we'll cache snapshots in the API layer rather than keep a
continuously-running ffmpeg here.

The class runs as an asyncio task during API lifespan: reconciles on
startup, then listens to Postgres NOTIFY 'cameras_changed' and reconciles
on every change.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

import asyncpg
import httpx
from baba_core.pg_listen import ResilientListener
from home_core.tasks import spawn

log = logging.getLogger(__name__)


def _desired_streams(cams) -> dict[str, list[str]]:
    """Exactly what the camera exposes, one source per name: "{slug}" is its
    stream_url and "{slug}_sub" its substream. go2rtc falls back to a name's
    second source when the first stalls and stays there, so a second source is
    a second stream wearing the first one's name."""
    desired: dict[str, list[str]] = {}
    for c in cams:
        desired[c["slug"]] = [c["stream_url"]]
        if c["substream_url"]:
            desired[f"{c['slug']}_sub"] = [c["substream_url"]]
    return desired


def _is_managed_name(name: str) -> bool:
    """Skip non-slug-shaped names so a manually-added "test" stream in
    go2rtc.yaml isn't reaped. Slugs match [a-z0-9][a-z0-9_-]* — anything else
    we treat as "not ours". `_sub` variants are included because they end in
    our managed suffix."""
    return bool(name) and name[0].isalnum() and name.replace("_", "").replace("-", "").isalnum()


class Go2RtcSync:
    def __init__(
        self, dsn: str, go2rtc_url: str, auth: tuple[str, str]
    ) -> None:
        self._dsn = dsn
        self._base = go2rtc_url.rstrip("/")
        self._auth = auth
        self._pool: asyncpg.Pool | None = None
        self._listener: ResilientListener | None = None
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=2, read=5, write=2, pool=5), auth=auth
        )
        self._reconcile_event = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._rtsp_auth_ok = False
        # What this process last PUT per name, for when go2rtc masks it.
        self._put: dict[str, list[str]] = {}

    async def start(self) -> None:
        self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=2)
        # Resilient LISTEN: initial reconcile + reconnect/re-reconcile after a
        # DB blip, so a camera added while the listener was down still gets
        # synced to go2rtc (the "added camera → black preview" bug).
        self._listener = ResilientListener(
            self._dsn,
            ["cameras_changed"],
            on_notify=self._on_notify,
            on_connect=self._reconcile,
            name="go2rtc-listen",
        )
        await self._listener.start()
        self._task = spawn(self._reconcile_loop(), name="go2rtc-sync", log=log)
        log.info("go2rtc sync started (target=%s)", self._base)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self._listener is not None:
            await self._listener.stop()
        if self._pool is not None:
            await self._pool.close()
        await self._client.aclose()

    def _on_notify(self, *_args) -> None:
        self._reconcile_event.set()

    async def _reconcile_loop(self) -> None:
        while True:
            try:
                await self._reconcile_event.wait()
                self._reconcile_event.clear()
                # Coalesce bursts (e.g. several quick PATCHes).
                await asyncio.sleep(0.3)
                await self._reconcile()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("go2rtc reconcile failed; will retry on next NOTIFY")

    @staticmethod
    def _rtsp_username_in_config(text: str) -> str | None:
        """Read `rtsp.username` out of go2rtc's YAML config without a YAML
        dependency. go2rtc emits canonical two-space YAML, so a top-level
        `rtsp:` followed by an indented `username:` is all we need to match."""
        in_rtsp = False
        for line in text.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if line[0] not in " \t":
                in_rtsp = line.strip().rstrip(":") == "rtsp"
                continue
            if in_rtsp and line.strip().startswith("username:"):
                return line.split("username:", 1)[1].strip().strip("\"'")
        return None

    async def _ensure_rtsp_auth(self) -> None:
        """Make go2rtc require credentials on its RTSP port.

        go2rtc runs network_mode:host, so without this any LAN host can pull
        every camera off :8554 unauthenticated. The RTSP listener reads its
        credentials once at load, so this GETs the running config and only when
        they are absent PATCHes them in and restarts go2rtc — idempotent, so a
        healthy install does nothing after the first boot. The api is already
        the sole writer of go2rtc's config (streams), so RTSP auth belongs here
        too rather than in a runtime yaml the operator would have to hand-edit
        on every existing install. Loopback is exempt in go2rtc, so the
        container's own healthcheck still passes. The credentials are the API
        ones (a single go2rtc identity); the ingestor and recorder carry the
        same pair in their pull URL (BABA_GO2RTC_RTSP)."""
        if self._rtsp_auth_ok:
            return
        user, password = self._auth
        try:
            r = await self._client.get(f"{self._base}/api/config")
            r.raise_for_status()
        except httpx.HTTPError as e:
            log.warning("go2rtc /api/config unreachable; RTSP auth not set yet: %s", e)
            return
        if self._rtsp_username_in_config(r.text) == user:
            self._rtsp_auth_ok = True
            return
        patch = f'rtsp:\n  username: "{user}"\n  password: "{password}"\n'
        try:
            r = await self._client.patch(f"{self._base}/api/config", content=patch)
            r.raise_for_status()
            await self._client.post(f"{self._base}/api/restart")
        except httpx.HTTPError as e:
            log.warning("go2rtc RTSP auth PATCH/restart failed: %s", e)
            return
        log.info("go2rtc RTSP authentication enabled; go2rtc restarting")
        # Wait for go2rtc to come back so the stream reconcile that follows
        # isn't racing a dead socket.
        for _ in range(20):
            await asyncio.sleep(0.5)
            with contextlib.suppress(httpx.HTTPError):
                if (await self._client.get(f"{self._base}/api/streams")).status_code == 200:
                    break
        self._rtsp_auth_ok = True

    async def _reconcile(self) -> None:
        assert self._pool is not None
        await self._ensure_rtsp_auth()
        async with self._pool.acquire() as conn:
            cams = await conn.fetch(
                "SELECT slug, stream_url, substream_url FROM cameras WHERE enabled"
            )

        # Fetch current go2rtc streams. Failing here is non-fatal — log + retry.
        try:
            r = await self._client.get(f"{self._base}/api/streams")
            r.raise_for_status()
            current: dict = r.json()
        except (httpx.HTTPError, ValueError) as e:
            log.warning("go2rtc /api/streams unreachable: %s", e)
            return

        desired_streams = _desired_streams(cams)

        # Add/update.
        moved: list[str] = []
        for name, desired_srcs in desired_streams.items():
            if await self._sync_stream(name, desired_srcs, current.get(name, {})):
                moved.append(name)

        # go2rtc's PUT swaps in a new stream object under the name and leaves
        # whoever is already reading it on the old source, for as long as they
        # stay connected. The recorder and the ingestor hang up and dial again
        # on this, and only after the new source is in place.
        if moved:
            async with self._pool.acquire() as conn:
                for name in moved:
                    await conn.execute("SELECT pg_notify('go2rtc_source_changed', $1)", name)

        # Delete streams that aren't enabled/known. We only delete streams that
        # match a *current* DB slug pattern; never touch streams added manually
        # outside our control unless they collide with a removed slug.
        for name in list(current.keys()):
            if name not in desired_streams and _is_managed_name(name):
                await self._delete_stream(name)

    async def _sync_stream(self, name: str, desired_srcs: list[str], existing) -> bool:
        """PUT the stream when go2rtc's sources differ from ours; True when a
        stream that already had a source was moved to a different one."""
        producers = existing.get("producers") if isinstance(existing, dict) else None
        current_srcs = [s for p in producers or () if (s := p.get("source") or p.get("url"))]
        # Compare ignoring order; if a producer URL is masked in go2rtc
        # response (***), we can't tell — so always PUT to be safe.
        masked = any("***" in s for s in current_srcs)
        if not masked and sorted(current_srcs) == sorted(desired_srcs):
            return False
        before = (self._put.get(name) if masked else current_srcs) if current_srcs else None
        params = [("name", name)] + [("src", s) for s in desired_srcs]
        try:
            r = await self._client.put(f"{self._base}/api/streams", params=params)
            r.raise_for_status()
            log.info("go2rtc: synced stream %s", name)
        except httpx.HTTPError as e:
            log.warning("go2rtc PUT %s failed: %s", name, e)
            return False
        self._put[name] = desired_srcs
        return before is not None and sorted(before) != sorted(desired_srcs)

    async def _delete_stream(self, name: str) -> None:
        try:
            r = await self._client.delete(f"{self._base}/api/streams", params={"src": name})
        except httpx.HTTPError as e:
            log.warning("go2rtc DELETE %s failed: %s", name, e)
            return
        if r.status_code in (200, 204):
            log.info("go2rtc: removed stream %s", name)
        else:
            log.warning("go2rtc DELETE %s -> %s", name, r.status_code)
