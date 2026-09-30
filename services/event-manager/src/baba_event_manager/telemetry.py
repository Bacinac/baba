"""Telemetry sink + deterministic watchers.

Sink: subscribes to `baba.telemetry.>` and persists every per-camera minute
bucket (detector / tracker / ingestor emit them) into `camera_telemetry` as
raw jsonb — the pipeline's flight recorder. Rows are small and bounded
(~cameras × sources per minute) and swept after RETENTION_DAYS.

Watchers: every WATCH_INTERVAL_S the watcher aggregates the last
WATCH_WINDOW_S of buckets per camera and opens a `telemetry_incidents` row
when a pattern trips — the deterministic trigger layer under the LLM
analysis (phase 3 fills verdict/suggestion; the operator applies or
dismisses). One open incident per (camera, kind); it closes automatically
once the metric falls below half its open threshold (hysteresis, so a
value oscillating around the trip point doesn't flap open/close).

Kinds — only two, and deliberately so. A watcher may ask only what no other
part of the pipeline already answers. Anything else re-derives a decision
that has already been made, against a second threshold, without the standing
to act on it.
- active_pinned — an adaptive camera (idle_fps set) spent >80% of the
  window at full rate while the tracker never saw a single ACTIVE track:
  the rate is being held up by noise (FP flicker, geometry jitter), not by
  real activity. This is exactly the "digne se fps — je li s razlogom?"
  question, pre-answered with evidence.
- drop_spike — detection rules are eating a large multiple of what gets
  published: a threshold is probably blinding the camera (the backyard
  min_box_pct=15% person incident, as a class). Under two-threshold tracking
  this sees below-MAINTAIN and size drops only.

birth_starved, flip_spike and track_churn were kinds here until 2026-09-09.
The tracker already counts births and flips per camera, and the phantom-spot
registry already suppresses a birth spot that never moves. Measured on this
deployment: patio/car stood at 1538 births with zero movers over 443 h — the
registry had been suppressing it for weeks while birth_starved reopened over
the same object every day, and nothing downstream could act on the incident.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import asyncpg
import msgspec
from baba_core.light_profiles import (
    apply_settings,
    classify_light,
    load_profile,
    record_change,
    snapshot_live,
)

log = logging.getLogger("baba.event-manager.telemetry")

RETENTION_DAYS = 30
WATCH_INTERVAL_S = 300
WATCH_WINDOW_S = 900

# Open thresholds; incidents close at half these values (hysteresis).
ACTIVE_PINNED_DUTY = 0.8  # fraction of window at full rate
ACTIVE_PINNED_MIN_COVERAGE_S = 600  # need ≥10 min of ingestor data
DROP_SPIKE_MIN = 150  # conf/size-dropped detections per window …
DROP_SPIKE_RATIO = 3.0  # … and ≥3× what got published
# After an operator dismisses (or applies a fix for) an incident, don't
# reopen the same (camera, kind) for this long even if the condition still
# trips — a standing scene condition the operator judged benign would
# otherwise resurrect every watch pass.
REOPEN_COOLDOWN_H = 24

# Illumination-band switch debounce: a new band must hold for this many
# consecutive ingestor buckets (~1 min each) before the stored profile is
# applied — headlights sweeping the scene or one odd bucket must not flip
# the setpoint schedule. classify_light() adds luma hysteresis on top.
LIGHT_DEBOUNCE_BUCKETS = 2


class TelemetrySink:
    """Persists telemetry buckets and runs the incident watchers."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool
        # Illumination-band tracking, fed by live ingestor buckets.
        self._light_state: dict[str, str] = {}  # slug → applied band (cache of cameras row)
        self._light_streak: dict[str, tuple[str, int]] = {}  # slug → (candidate, consecutive)

    async def handle(self, msg) -> None:
        # Subject: baba.telemetry.<source>.<camera_id>
        try:
            _, _, source, camera = msg.subject.split(".", 3)
            payload: dict[str, Any] = msgspec.msgpack.decode(msg.data)
        except Exception:
            log.exception("undecodable telemetry on %s", msg.subject)
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO camera_telemetry (at, camera_slug, source, payload)
                    VALUES (now(), $1, $2, $3::jsonb)
                    """,
                    camera,
                    source,
                    json.dumps(payload),
                )
        except Exception:
            log.exception("telemetry insert failed for %s", msg.subject)
            return
        if source == "ingestor":
            try:
                await self._track_light(camera, payload)
            except Exception:
                log.exception("light tracking failed for %s", camera)

    # ------------------------------------------------------------------
    # Illumination bands → setpoint schedule

    async def _track_light(self, slug: str, p: dict[str, Any]) -> None:
        luma = float(p.get("luma_avg", -1.0))
        ir = float(p.get("ir_ratio", -1.0))
        if luma < 0 and ir < 0:
            return
        current = self._light_state.get(slug)
        if current is None:
            current = await self._pool.fetchval(
                "SELECT light_condition FROM cameras WHERE slug = $1", slug
            )
            if current is None:
                return
            self._light_state[slug] = current
        candidate = classify_light(luma, max(ir, 0.0), current)
        if candidate is None or candidate == current:
            self._light_streak.pop(slug, None)
            return
        prev, n = self._light_streak.get(slug, (None, 0))
        n = n + 1 if prev == candidate else 1
        if n < LIGHT_DEBOUNCE_BUCKETS:
            self._light_streak[slug] = (candidate, n)
            return
        self._light_streak.pop(slug, None)
        if await self._held(slug):
            # A band measured on a picture WE changed is not the yard's band.
            # The headlight swap holds west in a monochrome profile for 45 s;
            # straddling two buckets that is two consecutive `ir` readings —
            # enough to switch the setpoint profile, and now enough to arm a
            # lamp — on nothing that happened outside.
            log.info(
                "light band cam=%s: %s → %s ignored, the camera is held in a "
                "temporary profile", slug, current, candidate,
            )
            return
        await self._switch_light(slug, candidate)

    async def _held(self, slug: str) -> bool:
        return bool(
            await self._pool.fetchval(
                "SELECT 1 FROM camera_profile_override o JOIN cameras c "
                "ON c.id = o.camera_id WHERE c.slug = $1",
                slug,
            )
        )

    async def _switch_light(self, slug: str, new: str) -> None:
        """The setpoint switch: record the new band on the camera and, if a
        validated profile exists for it, write it into the live tables
        (journaled). A band nobody has tuned yet changes nothing — the next
        incident under those conditions will teach it."""
        async with self._pool.acquire() as conn, conn.transaction():
            cam = await conn.fetchrow(
                "SELECT id, light_condition FROM cameras WHERE slug = $1 FOR UPDATE", slug
            )
            if cam is None:
                return
            old = cam["light_condition"]
            if old == new:
                self._light_state[slug] = new
                return
            await conn.execute(
                "UPDATE cameras SET light_condition = $2 WHERE id = $1", cam["id"], new
            )
            profile = await load_profile(conn, cam["id"], new)
            if profile is not None:
                before = await snapshot_live(conn, cam["id"])
                await apply_settings(conn, cam["id"], profile)
                await record_change(
                    conn,
                    cam["id"],
                    source="profile_switch",
                    light_condition=new,
                    before=before,
                    after=profile,
                )
        self._light_state[slug] = new
        log.info(
            "light band cam=%s: %s → %s%s",
            slug,
            old,
            new,
            " — stored profile applied"
            if profile is not None
            else " — no stored profile, settings unchanged",
        )

    async def retention_loop(self) -> None:
        while True:
            await asyncio.sleep(3600)
            try:
                async with self._pool.acquire() as conn:
                    res = await conn.execute(
                        f"DELETE FROM camera_telemetry "  # noqa: S608
                        f"WHERE at < now() - interval '{RETENTION_DAYS} days'"
                    )
                log.info("telemetry retention sweep: %s", res)
            except Exception:
                log.exception("telemetry retention sweep failed")

    # ------------------------------------------------------------------
    # Watchers

    async def watch_loop(self) -> None:
        while True:
            await asyncio.sleep(WATCH_INTERVAL_S)
            try:
                await self._watch_pass()
            except Exception:
                log.exception("telemetry watch pass failed")

    async def _watch_pass(self) -> None:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT camera_slug, source, payload
                FROM camera_telemetry
                WHERE at > now() - make_interval(secs => $1)
                """,
                float(WATCH_WINDOW_S),
            )
            cams = {
                r["slug"]: r
                for r in await conn.fetch(
                    "SELECT slug, id, light_condition, idle_fps FROM cameras WHERE enabled"
                )
            }
            adaptive = {slug for slug, r in cams.items() if r["idle_fps"] is not None}
            # Live incidents (open/analyzed) can be auto-closed when the
            # condition clears; anything the operator already acted on
            # (dismissed/applied) or that auto-closed recently blocks a
            # reopen for the cooldown window.
            live_incidents = {
                (r["camera_slug"], r["kind"]): r["id"]
                for r in await conn.fetch(
                    "SELECT id, camera_slug, kind FROM telemetry_incidents "
                    "WHERE status IN ('open', 'analyzed')"
                )
            }
            blocked = set(live_incidents) | {
                (r["camera_slug"], r["kind"])
                for r in await conn.fetch(
                    "SELECT camera_slug, kind FROM telemetry_incidents "
                    "WHERE opened_at > now() - make_interval(hours => $1)",
                    REOPEN_COOLDOWN_H,
                )
            }

            metrics = self._aggregate(rows)
            for camera, m in metrics.items():
                # Incidents are a per-camera concept. The table also carries
                # host-wide rows (hwstats writes GPU/CPU buckets under a
                # sentinel slug) and can hold buckets of a camera that has
                # since been deleted — neither has a scene to diagnose.
                if camera not in cams:
                    continue
                for kind, (tripped, cleared, details) in self._evaluate(
                    camera, m, camera in adaptive
                ).items():
                    key = (camera, kind)
                    incident_id = live_incidents.get(key)
                    if tripped and key not in blocked:
                        # Stamp the "before" side of the correlation: which
                        # illumination band this happened under and what the
                        # settings were when the pattern tripped.
                        cam_row = cams.get(camera)
                        if cam_row is not None:
                            details = {
                                **details,
                                "light_condition": cam_row["light_condition"],
                                "settings_before": await snapshot_live(conn, cam_row["id"]),
                            }
                        await conn.execute(
                            """
                            INSERT INTO telemetry_incidents (camera_slug, kind, details)
                            VALUES ($1, $2, $3::jsonb)
                            """,
                            camera,
                            kind,
                            json.dumps(details),
                        )
                        log.warning("incident opened: cam=%s kind=%s %s", camera, kind, details)
                    elif cleared and incident_id is not None:
                        await conn.execute(
                            """
                            UPDATE telemetry_incidents
                            SET closed_at = now(), status = 'closed',
                                details = details || jsonb_build_object('closing', $2::jsonb)
                            WHERE id = $1 AND status IN ('open', 'analyzed')
                            """,
                            incident_id,
                            json.dumps(details),
                        )
                        log.info("incident closed: cam=%s kind=%s", camera, kind)

    @staticmethod
    def _aggregate(rows) -> dict[str, dict[str, Any]]:
        """Window totals per camera across the three sources."""
        out: dict[str, dict[str, Any]] = {}
        for r in rows:
            m = out.setdefault(
                r["camera_slug"],
                {
                    "active_s": 0.0,
                    "idle_s": 0.0,
                    "transitions": 0,
                    "max_active": 0,
                    "max_parked": 0,
                    "published": 0,
                    "dropped": 0,
                    "dropped_by_class": {},
                    "luma": [],
                    "ir": [],
                },
            )
            p = json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"]
            if r["source"] == "ingestor":
                m["active_s"] += p.get("active_s", 0.0)
                m["idle_s"] += p.get("idle_s", 0.0)
                m["transitions"] += p.get("transitions", 0)
                if p.get("luma_avg", -1.0) >= 0:
                    m["luma"].append(p["luma_avg"])
                if p.get("ir_ratio", -1.0) >= 0:
                    m["ir"].append(p["ir_ratio"])
            elif r["source"] == "tracker":
                m["max_active"] = max(m["max_active"], p.get("n_active", 0))
                m["max_parked"] = max(m["max_parked"], p.get("n_parked", 0))
            elif r["source"] == "detector":
                for stats in p.get("published", {}).values():
                    m["published"] += stats.get("n", 0)
                for cls, reasons in p.get("dropped", {}).items():
                    # Disabled-class drops are by design (the operator turned
                    # that class off) — a permanent stream of them on a static
                    # scene must not hold drop_spike open. Only conf/size
                    # drops can silently blind a class someone wants.
                    n = reasons.get("conf", 0) + reasons.get("size", 0)
                    m["dropped"] += n
                    if n:
                        m["dropped_by_class"][cls] = m["dropped_by_class"].get(cls, 0) + n
        return out

    @staticmethod
    def _evaluate(
        camera: str, m: dict[str, Any], is_adaptive: bool
    ) -> dict[str, tuple[bool, bool, dict[str, Any]]]:
        """kind → (tripped, cleared, evidence). Clear thresholds sit at half
        the open thresholds so a metric hovering at the line doesn't flap."""
        verdicts: dict[str, tuple[bool, bool, dict[str, Any]]] = {}

        # Measured scene light rides along in every incident's evidence —
        # day vs night-IR changes which FP patterns are plausible, so the
        # analysis (and the operator) should never have to guess it.
        light: dict[str, Any] = {}
        if m["luma"]:
            light["luma_avg"] = round(sum(m["luma"]) / len(m["luma"]))
        if m["ir"]:
            light["ir_ratio"] = round(sum(m["ir"]) / len(m["ir"]), 2)

        coverage = m["active_s"] + m["idle_s"]
        duty = m["active_s"] / coverage if coverage > 0 else 0.0
        if is_adaptive:
            details = {
                "duty": round(duty, 3),
                "coverage_s": round(coverage),
                "transitions": m["transitions"],
                "max_active_tracks": m["max_active"],
                "max_parked_tracks": m["max_parked"],
                "window_s": WATCH_WINDOW_S,
                **light,
            }
            tripped = (
                coverage >= ACTIVE_PINNED_MIN_COVERAGE_S
                and duty > ACTIVE_PINNED_DUTY
                and m["max_active"] == 0
            )
            cleared = duty < ACTIVE_PINNED_DUTY / 2 or m["max_active"] > 0
            verdicts["active_pinned"] = (tripped, cleared, details)

        details = {
            "published": m["published"],
            "dropped": m["dropped"],
            "dropped_by_class": m["dropped_by_class"],
            "window_s": WATCH_WINDOW_S,
            **light,
        }
        tripped = m["dropped"] >= DROP_SPIKE_MIN and m["dropped"] > DROP_SPIKE_RATIO * max(
            m["published"], 1
        )
        cleared = m["dropped"] < DROP_SPIKE_MIN / 2
        verdicts["drop_spike"] = (tripped, cleared, details)

        return verdicts
