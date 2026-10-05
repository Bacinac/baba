"""Server-Sent Events feeds for the live UI (detections, tracks, events,
identities).

These replace the earlier /ws/* WebSocket feeds. All four are one-way
server->client push, but a browser WebSocket upgrade does NOT survive the
Cloudflare tunnel (the same reason live video moved to HTTP fMP4) — the
detection overlay stayed black behind the tunnel. SSE is a plain long-lived
HTTP GET, so it passes straight through Cloudflare, and EventSource reconnects on
its own, which also simplifies the client. Auth is the ordinary session cookie
(EventSource sends it same-origin), so these use the same `current_user`
dependency as every other HTTP route instead of a bespoke WS handshake.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator

import msgspec
from baba_core.nats_conn import connect as nats_connect
from baba_core.nats_conn import drain_quietly
from baba_core.pg_listen import ResilientListener
from baba_core.wire import DetectionsMessage, TracksMessage
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from baba_api.auth import current_user
from baba_api.phantom_view import PhantomView

log = logging.getLogger(__name__)

sse_router = APIRouter()

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # Stop nginx / any buffering proxy from holding the stream back.
    "X-Accel-Buffering": "no",
}
# Comment-line heartbeat, so a proxy doesn't idle out a sparse stream (events,
# identities). It is NOT a disconnect detector — see `_detach` for how a client
# going away is actually noticed.
_HEARTBEAT_S = 20.0

def _detach(request: Request, coro) -> None:
    """Run a stream's teardown OUTSIDE the generator that is being torn down.

    Starlette closes the generator when the client hangs up, so the `finally`
    IS entered — but the generator is being finalised, and every `await` inside
    it raises CancelledError at once. CancelledError is a BaseException, so
    `suppress(Exception)` did not catch it: the very first cleanup await
    aborted the block, the drain never ran, and the connection stayed open
    for the lifetime of the process. Silent, because the log line sat after the
    awaits and never ran either.

    Measured on .11 with this exact uvicorn/starlette pair: three streams
    opened and closed, three NATS connections still held, zero disconnect lines
    logged — on the detections feed and equally on tracks/events, so Postgres
    was drifting toward its cap the same way. `request.is_disconnected()` is no
    help here: it never returns True in this stack, on a clean close or a hard
    reset. Handing the coroutine to a FRESH task is what works, because that
    task is not the thing being cancelled.
    """
    request.app.state.background_tasks.spawn(coro, name="sse-teardown")


async def _drain_nats(sub, nc, subject: str) -> None:
    with contextlib.suppress(Exception):
        await sub.unsubscribe()
    await drain_quietly(nc)
    log.info("sse client disconnected, subject=%s", subject)


# The wire structs are IMPORTED, never re-declared. Hand-copies here had
# already drifted: TrackWire has since gained ever_moved, first_seen_ns,
# last_matched_ns, identity_gid, identity_name and continues_track_id, and
# because msgspec ignores unknown keys the copy dropped them in silence — so
# /sse/tracks was quietly withholding the live identity name and the
# visit-continuation link from any consumer that asked for them.


async def _nats_sse(
    request: Request, subject: str, to_json, *, name: str, tick=None
) -> AsyncIterator[str]:
    """Subscribe to a NATS subject; yield each message as an SSE `data:` frame.
    A ~20s heartbeat comment keeps proxies from idling us out.

    `tick` is an optional async callback run once per loop pass, for state a
    serialiser needs but cannot fetch itself (`to_json` is synchronous — it
    runs inside the NATS handler). This generator is the only async context
    whose lifetime is exactly the stream's, so refreshes belong here rather
    than in a background task that would have to be cancelled by hand."""
    nc = await nats_connect(request.app.state.config.nats_url, name=name)
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=64)

    async def handler(msg) -> None:
        try:
            payload = to_json(msg.data)
        except Exception:
            log.exception("sse decode error")
            return
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            # Slow client — drop oldest, keep freshest. UI wants freshness.
            with contextlib.suppress(Exception):
                queue.get_nowait()
                queue.put_nowait(payload)

    # Drain the connection if the subscribe itself fails — otherwise this
    # already-open NATS connection leaks (never drained) on every setup error.
    try:
        sub = await nc.subscribe(subject, cb=handler)
    except Exception:
        await drain_quietly(nc)
        raise
    log.info("sse client connected, subject=%s", subject)
    try:
        while True:
            if tick is not None:
                with contextlib.suppress(Exception):
                    await tick()
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_S)
            except TimeoutError:
                yield ": ping\n\n"
                continue
            yield f"data: {payload}\n\n"
    finally:
        _detach(request, _drain_nats(sub, nc, subject))


class _NotifyHub:
    """One process-wide Postgres LISTEN per channel, fanned out to every SSE
    client subscribed to it.

    The naive design (one ResilientListener per client) opened a dedicated
    asyncpg connection per browser tab. Postgres' default max_connections is
    100, shared across ALL BABA services — so a couple dozen SSE clients could
    exhaust the pool and wedge the whole stack (recorder can't index, detector
    can't reload rules). Here N clients on a channel share ONE connection; the
    listener is started on the first subscriber and stopped when the last one
    leaves, so an idle server holds zero LISTEN connections."""

    def __init__(self, dsn: str, channel: str, name: str) -> None:
        self._dsn = dsn
        self._channel = channel
        self._name = name
        self._subscribers: set[asyncio.Queue[str]] = set()
        self._listener: ResilientListener | None = None
        self._lock = asyncio.Lock()

    def _on_notify(self, _channel: str, payload: str) -> None:
        # Iterate a snapshot: subscribe/unsubscribe mutate the set (both run on
        # this same loop, but only defensively). Slow consumer → drop; the
        # frontend refetches the canonical list.
        for q in list(self._subscribers):
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(payload)

    async def subscribe(self) -> asyncio.Queue[str]:
        async with self._lock:
            if self._listener is None:
                self._listener = ResilientListener(
                    self._dsn, [self._channel], on_notify=self._on_notify, name=self._name
                )
                await self._listener.start()
            q: asyncio.Queue[str] = asyncio.Queue(maxsize=256)
            self._subscribers.add(q)
            return q

    async def unsubscribe(self, q: asyncio.Queue[str]) -> None:
        async with self._lock:
            self._subscribers.discard(q)
            if not self._subscribers and self._listener is not None:
                listener, self._listener = self._listener, None
                with contextlib.suppress(Exception):
                    await listener.stop()


_notify_hubs: dict[str, _NotifyHub] = {}
_notify_hubs_lock = asyncio.Lock()


async def _get_notify_hub(dsn: str, channel: str, name: str) -> _NotifyHub:
    async with _notify_hubs_lock:
        hub = _notify_hubs.get(channel)
        if hub is None:
            hub = _NotifyHub(dsn, channel, name)
            _notify_hubs[channel] = hub
        return hub


async def _release_hub(hub: _NotifyHub, queue: asyncio.Queue[str], name: str) -> None:
    with contextlib.suppress(Exception):
        await hub.unsubscribe(queue)
    log.info("%s client disconnected", name)


async def _notify_sse(request: Request, channel: str, *, name: str) -> AsyncIterator[str]:
    """Relay Postgres NOTIFY payloads on `channel` as SSE frames, sharing one
    process-wide LISTEN connection per channel across all clients (see
    _NotifyHub) instead of one connection per browser tab."""
    hub = await _get_notify_hub(request.app.state.config.dsn, channel, name)
    queue = await hub.subscribe()
    log.info("%s client connected", name)
    try:
        while True:
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_S)
            except TimeoutError:
                yield ": ping\n\n"
                continue
            yield f"data: {payload}\n\n"
    finally:
        _detach(request, _release_hub(hub, queue, name))


@sse_router.get("/sse/detections")
async def detections_sse(
    request: Request, camera: str | None = None, _user=Depends(current_user)
) -> StreamingResponse:
    """Live detection messages. Optional `?camera=<slug>` filters server-side.

    This is the detector's output — deliberately upstream of the tracker, so
    the zone editor shows what the model says rather than what survived. The
    cost of that honesty is that the feed also carries boxes the pipeline is
    already discarding, and the operator has no way to tell which. `phantom`
    closes the gap for the suppression that fires day to day: a box landing on
    a learned static-phantom spot is annotated with the evidence behind it, and
    the editor greys it out. Only meaningful per camera, so it rides on
    `?camera=` — the unfiltered form has no single registry to resolve
    against.
    """
    subject = f"baba.detections.{camera}" if camera else "baba.detections.*"
    decoder = msgspec.msgpack.Decoder(DetectionsMessage)

    phantoms: PhantomView | None = None
    if camera:
        phantoms = PhantomView(request.app.state.pool, camera)
        # Load before the first frame, so the preview never opens with one
        # unannotated pass that flashes a phantom as live.
        await phantoms.refresh_if_stale()

    def to_json(data: bytes) -> str:
        w = decoder.decode(data)
        fw = w.frame_width or 1
        fh = w.frame_height or 1
        out: list[dict] = []
        for d in w.detections:
            item = {
                "x1": d.x1,
                "y1": d.y1,
                "x2": d.x2,
                "y2": d.y2,
                "class_id": d.class_id,
                "class_name": d.class_name,
                "confidence": d.confidence,
                # birth_eligible rides along so the editor can tell "would
                # start a track" from "only keeps one alive".
                "birth_eligible": d.birth_eligible,
            }
            if phantoms is not None:
                # The registry is normalised so an ingestor downscale change
                # can't orphan it; detection coords are in frame pixels.
                spot = phantoms.match(
                    d.class_name, (d.x1 / fw, d.y1 / fh, d.x2 / fw, d.y2 / fh), d.confidence
                )
                if spot is not None:
                    item["phantom"] = {"births": spot.births, "span_h": round(spot.span_h, 1)}
            out.append(item)
        return json.dumps(
            {
                "camera_id": w.camera_id,
                "sequence": w.sequence,
                "timestamp_ns": w.timestamp_ns,
                "frame_width": w.frame_width,
                "frame_height": w.frame_height,
                "detections": out,
            }
        )

    return StreamingResponse(
        _nats_sse(
            request,
            subject,
            to_json,
            name=f"api-sse-det-{id(request):x}",
            tick=phantoms.refresh_if_stale if phantoms is not None else None,
        ),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@sse_router.get("/sse/tracks")
async def tracks_sse(
    request: Request, camera: str | None = None, _user=Depends(current_user)
) -> StreamingResponse:
    """Live tracker output (bbox + class + confidence + motion_state per track).
    Optional `?camera=<slug>` filters server-side."""
    subject = f"baba.tracks.{camera}" if camera else "baba.tracks.*"
    decoder = msgspec.msgpack.Decoder(TracksMessage)

    def to_json(data: bytes) -> str:
        w = decoder.decode(data)
        return json.dumps(
            {
                "camera_id": w.camera_id,
                "sequence": w.sequence,
                "timestamp_ns": w.timestamp_ns,
                "frame_width": w.frame_width,
                "frame_height": w.frame_height,
                "tracks": [
                    {
                        "track_id": t.track_id,
                        "x1": t.x1,
                        "y1": t.y1,
                        "x2": t.x2,
                        "y2": t.y2,
                        "class_id": t.class_id,
                        "class_name": t.class_name,
                        "confidence": t.confidence,
                        "motion_state": t.motion_state,
                        "state_since_ns": t.state_since_ns,
                        "ever_moved": t.ever_moved,
                        "identity_gid": t.identity_gid,
                        "identity_name": t.identity_name,
                        "continues_track_id": t.continues_track_id,
                    }
                    for t in w.tracks
                ],
            }
        )

    return StreamingResponse(
        _nats_sse(request, subject, to_json, name=f"api-sse-trk-{id(request):x}"),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@sse_router.get("/sse/events")
async def events_sse(request: Request, _user=Depends(current_user)) -> StreamingResponse:
    """Durable events live, backed by Postgres NOTIFY 'events_new'."""
    return StreamingResponse(
        _notify_sse(request, "events_new", name="sse/events"),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@sse_router.get("/sse/identities")
async def identities_sse(
    request: Request, _user=Depends(current_user)
) -> StreamingResponse:
    """Identity-level activity (merge / split / auto_match), backed by NOTIFY
    'identities_changed'. UI debounces a refetch of the affected view."""
    return StreamingResponse(
        _notify_sse(request, "identities_changed", name="sse/identities"),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )
