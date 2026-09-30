"""Background task: LISTEN events_new → match rules → dispatch.

Runs inside the api process during lifespan. We use a dedicated asyncpg
connection (not the pool) because asyncpg's LISTEN handler is bound to
a single connection and we want the pool free for request handling.

The matcher is intentionally narrow for v1 (event_kind + filter on
camera_ids / class_ids). New filter dimensions plug in via the
`_matches_filter` function — no signature change to the rest of the
loop. Notification deliveries are fan-out per channel; one failed
channel doesn't block the others on the same rule.

Why here and not in event-manager:
  - event-manager already has too many concerns (re-ID kNN, thumbnail
    capture, sample cleanup); adding outbound HTTP + SMTP increases its
    failure surface for a feature that's API-config-driven.
  - The api process owns the secret_key + Fernet (channels are stored
    encrypted), so decrypting channel configs in api avoids a second
    place that needs the key.
  - Postgres LISTEN scales fine for v1 event rates (single-digit per
    minute typical, hundreds per minute peak); if that changes we move
    this to a worker tier without changing rule semantics.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from datetime import UTC, datetime
from typing import Any

import asyncpg
from baba_core.pg_listen import ResilientListener
from home_core.rate_limit import TokenBucketLimiter
from home_core.tasks import spawn

from baba_api.crypto import decrypt_secret
from baba_api.notifications import NotificationError, dispatch

log = logging.getLogger(__name__)


# Max time we wait per channel before giving up. The send adapters
# already have their own 5s socket timeouts; this is a safety belt for
# the rare case an adapter blocks longer than expected (e.g. SMTP
# thread-pool saturation).
_CHANNEL_TIMEOUT_S = 15.0


# Per-channel rate limiters, cached by (channel_id, rate_limit_per_min).
# Keying on the rate too means a CRUD update that changes the limit
# auto-invalidates the bucket — the next attempt mints a fresh one with
# the new capacity. Module-level so the cache survives across dispatch
# tasks; on a single-process api this is the simplest correctness model.
_channel_buckets: dict[tuple[str, int], TokenBucketLimiter] = {}


def _get_channel_bucket(channel_id: str, rate_per_min: int) -> TokenBucketLimiter:
    key = (channel_id, rate_per_min)
    b = _channel_buckets.get(key)
    if b is None:
        # Capacity = burst tolerance, refill = sustained rate per second.
        # We use capacity == rate so the first burst can drain the whole
        # minute's quota at once (operator probably wants the alarm
        # delivered now, not paced over a minute).
        b = TokenBucketLimiter(
            capacity=rate_per_min,
            refill_per_s=rate_per_min / 60.0,
            max_keys=4,  # at most one key used per channel
        )
        _channel_buckets[key] = b
    return b


def _matches_filter(filter_: dict[str, Any], event: dict[str, Any]) -> bool:
    """Return True if `event` satisfies all dimensions in `filter_`.
    Unknown dimensions are treated as "match nothing" — see the
    conservative-validator note in routes_rules.py. v1 dimensions:
      camera_ids: list[uuid string] → event.camera_id ∈ set
      class_ids:  list[int]         → event.track_class_id ∈ set
    """
    cam_ids = filter_.get("camera_ids")
    if cam_ids and str(event["camera_id"]) not in {str(c) for c in cam_ids}:
        return False
    class_ids = filter_.get("class_ids")
    if class_ids:
        track_cid = event.get("track_class_id")
        if track_cid is None or int(track_cid) not in {int(c) for c in class_ids}:
            return False
    return True


def _build_payload(event: dict[str, Any]) -> dict[str, Any]:
    """Render a notification-friendly payload from an event row."""
    class_name = event.get("track_class_name") or "?"
    cam_name = event.get("camera_name") or "?"
    title = f"BABA: {class_name} on {cam_name}"
    at: datetime = event["at"]
    return {
        "title": title,
        "subject": title,
        "body": (
            f"Event {event['kind']} on camera {cam_name} at {at.isoformat(timespec='seconds')}."
        ),
        "event_id": str(event["id"]),
        "camera_id": str(event["camera_id"]),
        "kind": event["kind"],
        "class": class_name,
        "at": at.isoformat(),
    }


async def _fetch_event(pool: asyncpg.Pool, event_id: str) -> dict[str, Any] | None:
    """Fetch the event row joined with camera name + track class_name/id.
    The NOTIFY payload only carries id/camera_id/kind/at; we need the
    class for filter matching, and the camera/class names for the
    notification body."""
    row = await pool.fetchrow(
        """
        SELECT e.id, e.kind, e.at, e.camera_id,
               c.name AS camera_name,
               t.class_id AS track_class_id,
               t.class_name AS track_class_name
        FROM events e
        JOIN cameras c ON c.id = e.camera_id
        LEFT JOIN tracks t ON t.id = e.track_id
        WHERE e.id = $1::uuid
        """,
        event_id,
    )
    if row is None:
        return None
    return dict(row)


async def _dispatch_event(
    pool: asyncpg.Pool,
    secret_key: str,
    event: dict[str, Any],
) -> None:
    """Evaluate rules for this event + fan out deliveries."""
    # Fetch enabled rules that match the event_kind (or are kind-agnostic).
    rules = await pool.fetch(
        """
        SELECT id, name, event_kind, filter, channel_ids
        FROM notification_rules
        WHERE enabled
          AND (event_kind IS NULL OR event_kind = $1)
        """,
        event["kind"],
    )
    if not rules:
        return

    for rule in rules:
        raw_filter = rule["filter"]
        if isinstance(raw_filter, str):
            flt = json.loads(raw_filter)
        else:
            flt = dict(raw_filter) if raw_filter is not None else {}
        if not _matches_filter(flt, event):
            continue

        channel_ids = list(rule["channel_ids"] or [])
        if not channel_ids:
            continue

        channels = await pool.fetch(
            """
            SELECT id, kind, config_encrypted, rate_limit_per_min
            FROM notification_channels
            WHERE id = ANY($1::uuid[]) AND enabled
            """,
            channel_ids,
        )
        if not channels:
            # All referenced channels are disabled or gone — record
            # something useful so the operator notices instead of
            # silently dropping notifications.
            await pool.execute(
                "UPDATE notification_rules SET last_fired_at = $1, last_error = $2 WHERE id = $3",
                datetime.now(UTC),
                "rule matched event but no active channels found",
                rule["id"],
            )
            continue

        payload = _build_payload(event)
        last_error: str | None = None
        for ch in channels:
            # Per-channel rate limit (notification_channels.rate_limit_per_min).
            # 0 = unlimited. When the bucket is dry we still record the
            # delivery (so the operator can see why alerts dropped) but
            # skip the actual dispatch so a runaway zone doesn't flood
            # Slack / Telegram.
            limit = int(ch["rate_limit_per_min"] or 0)
            if limit > 0:
                bucket = _get_channel_bucket(str(ch["id"]), limit)
                if not bucket.take(str(ch["id"])):
                    log.debug(
                        "channel %s rate limited (%d/min); dropping notification",
                        ch["id"],
                        limit,
                    )
                    await _record_delivery(
                        pool,
                        rule_id=rule["id"],
                        channel_id=ch["id"],
                        event_id=event["id"],
                        event_kind=event["kind"],
                        channel_kind=ch["kind"],
                        ok=False,
                        error=f"rate_limited ({limit}/min)",
                        duration_ms=0,
                    )
                    last_error = f"channel {ch['id']} rate limited"
                    continue

            try:
                config = json.loads(
                    decrypt_secret(bytes(ch["config_encrypted"]), secret_key),
                )
            except ValueError as e:
                msg = f"channel {ch['id']} config decrypt failed: {e}"
                log.warning("rule %s: %s", rule["id"], msg)
                last_error = msg
                await _record_delivery(
                    pool,
                    rule_id=rule["id"],
                    channel_id=ch["id"],
                    event_id=event["id"],
                    event_kind=event["kind"],
                    channel_kind=ch["kind"],
                    ok=False,
                    error=msg,
                    duration_ms=None,
                )
                continue
            start_mono = time.monotonic()
            try:
                await asyncio.wait_for(
                    dispatch(ch["kind"], config, payload),
                    timeout=_CHANNEL_TIMEOUT_S,
                )
            except (TimeoutError, NotificationError) as e:
                duration_ms = int((time.monotonic() - start_mono) * 1000)
                msg = f"channel {ch['id']} ({ch['kind']}): {e}"
                log.warning("rule %s: %s", rule["id"], msg)
                last_error = msg
                # Per-delivery audit row first so the failure is in
                # the timeline even if the live-status UPDATE below
                # fails too. Latency + error keep the operator's
                # first-stop investigation (channel page or audit
                # page) honest.
                await _record_delivery(
                    pool,
                    rule_id=rule["id"],
                    channel_id=ch["id"],
                    event_id=event["id"],
                    event_kind=event["kind"],
                    channel_kind=ch["kind"],
                    ok=False,
                    error=str(e),
                    duration_ms=duration_ms,
                )
                # Stamp the channel's last_error too so the channels
                # page reflects the failure (operator's first stop is
                # usually the channel detail, not the rule).
                try:
                    await pool.execute(
                        "UPDATE notification_channels SET last_used_at = $1, "
                        "last_error = $2 WHERE id = $3",
                        datetime.now(UTC),
                        str(e),
                        ch["id"],
                    )
                except Exception:
                    log.exception("rule %s: failed to stamp channel error", rule["id"])
            else:
                duration_ms = int((time.monotonic() - start_mono) * 1000)
                await _record_delivery(
                    pool,
                    rule_id=rule["id"],
                    channel_id=ch["id"],
                    event_id=event["id"],
                    event_kind=event["kind"],
                    channel_kind=ch["kind"],
                    ok=True,
                    error=None,
                    duration_ms=duration_ms,
                )
                # Success — clear the channel's last_error if it was set.
                try:
                    await pool.execute(
                        "UPDATE notification_channels SET last_used_at = $1, "
                        "last_error = NULL WHERE id = $2",
                        datetime.now(UTC),
                        ch["id"],
                    )
                except Exception:
                    log.exception("rule %s: failed to stamp channel success", rule["id"])

        await pool.execute(
            "UPDATE notification_rules SET last_fired_at = $1, last_error = $2 WHERE id = $3",
            datetime.now(UTC),
            last_error,
            rule["id"],
        )


async def _record_delivery(
    pool: asyncpg.Pool,
    *,
    rule_id: Any,
    channel_id: Any,
    event_id: Any,
    event_kind: str,
    channel_kind: str,
    ok: bool,
    error: str | None,
    duration_ms: int | None,
) -> None:
    """Append-only audit of a single delivery attempt. Best-effort —
    a failure here is logged but never raised so the dispatcher loop
    stays responsive even if the audit table is temporarily unreachable
    (e.g. during a VACUUM FULL)."""
    try:
        await pool.execute(
            """
            INSERT INTO notification_deliveries
                (rule_id, channel_id, event_id, event_kind, channel_kind,
                 ok, error, duration_ms)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            rule_id,
            channel_id,
            event_id,
            event_kind,
            channel_kind,
            ok,
            error,
            duration_ms,
        )
    except Exception:
        log.exception("failed to record delivery row (continuing)")


class RulesDispatcher:
    """Owns a dedicated asyncpg connection + LISTEN handler. Started
    from the api lifespan; stop() drains gracefully."""

    def __init__(self, dsn: str, pool: asyncpg.Pool, secret_key: str) -> None:
        self._dsn = dsn
        self._pool = pool
        self._secret_key = secret_key
        self._listener: ResilientListener | None = None
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=512)
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        # Dedicated LISTEN connection (kept off the HTTP pool). Resilient so a
        # DB blip doesn't permanently kill alerting — the listener reconnects
        # and notifications resume. No on_connect reconcile: event NOTIFYs are
        # transient, so any events emitted during the gap are simply not
        # alerted on (acceptable vs. the old failure mode of silent-dead).
        self._listener = ResilientListener(
            self._dsn,
            ["events_new"],
            on_notify=self._on_notify,
            name="rules-dispatcher-listen",
        )
        await self._listener.start()
        self._task = spawn(self._worker(), name="rules-dispatcher", log=log)
        log.info("rules dispatcher started")

    async def stop(self) -> None:
        self._stop.set()
        if self._listener is not None:
            await self._listener.stop()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    def _on_notify(self, channel: str, payload: str) -> None:
        # NOTIFY callbacks run on the connection's dispatch task — must
        # not block. Push the raw payload to the worker queue and let
        # the worker do the (async) DB + delivery work. Drop on full
        # so a stuck downstream doesn't back-pressure into Postgres'
        # NOTIFY buffer.
        try:
            self._queue.put_nowait(payload)
        except asyncio.QueueFull:
            log.warning(
                "rules dispatcher queue full (%d) — dropping notify",
                self._queue.qsize(),
            )

    async def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                payload = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except TimeoutError:
                continue
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                log.warning("rules dispatcher: malformed notify payload %r", payload[:200])
                continue
            event_id = data.get("id")
            if not event_id:
                continue
            try:
                event = await _fetch_event(self._pool, event_id)
                if event is None:
                    # Event vanished between NOTIFY and fetch — possible
                    # if a cascading delete fired right after insert.
                    continue
                await _dispatch_event(self._pool, self._secret_key, event)
            except Exception:
                log.exception(
                    "rules dispatcher: unexpected error processing event %s",
                    event_id,
                )
