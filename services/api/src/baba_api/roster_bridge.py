"""Publish BABA's full camera + zone roster onto NATS for off-box consumers (DIDA).

The `events_bridge` mirrors live *events* (someone crossed a zone), but DIDA also
wants the static *inventory*: every camera, every zone (even ones nobody has
entered yet), and which object classes each zone is configured to track. That
lets DIDA show the complete set of zones as entities up front, instead of only
the ones that have happened to fire an event.

This is the roster counterpart of the events bridge: it LISTENs on the
`cameras_changed` / `zones_changed` / `detection_rules_changed` NOTIFY channels,
and on connect + on every change republishes the whole roster to `baba.roster`
(a single self-contained message — the consumer replaces its view wholesale).
`detection_rules_changed` matters because per-camera/global class toggles change
detect_classes WITHOUT touching cameras/zones. Best-effort: if NATS is down it
logs and drops; the next change (or a reconnect) re-sends.
"""

from __future__ import annotations

import json
import logging

import asyncpg
from baba_core.pg_listen import ResilientListener
from baba_core.task_owner import TaskOwner

log = logging.getLogger(__name__)

_SUBJECT = "baba.roster"
_REQUEST = "baba.roster.get"  # request/reply so a consumer that connects LATER can pull it


class RosterNatsBridge:
    def __init__(self, dsn: str, pool: asyncpg.Pool, nc) -> None:
        self._dsn = dsn
        self._pool = pool
        self._nc = nc
        self._listener: ResilientListener | None = None
        self._tasks = TaskOwner("api-roster-bridge", log)

    async def start(self) -> None:
        # Serve on-demand: a DIDA adapter that (re)starts after us gets the roster
        # by request, not just the one-shot publish it would otherwise miss.
        await self._nc.subscribe(_REQUEST, cb=self._on_request)
        self._listener = ResilientListener(
            self._dsn,
            # `detection_rules_changed` too: a per-camera/global class enable/disable
            # (e.g. turning `person` on for one camera) changes detect_classes in the
            # roster but fires NEITHER cameras_changed NOR zones_changed — so without
            # this the roster went stale until a consumer reconnected, and DIDA kept
            # showing the pre-change class set. The detector already listens on this
            # same channel (rules.py); the roster must too.
            # `scene_regions_changed` too: scene regions and their state
            # vocabulary are part of the catalog DIDA builds entities from, and
            # they are declared here rather than validated against a fixed enum
            # (state labels are per-region and operator-defined — open/closed on
            # a gate, present/empty on a parking spot).
            [
                "cameras_changed",
                "zones_changed",
                "detection_rules_changed",
                "scene_regions_changed",
            ],
            on_notify=self._on_notify,
            on_connect=self._publish,  # full roster on initial connect + every reconnect
            name="api-roster-bridge",
        )
        await self._listener.start()
        log.info("roster→NATS bridge listening on cameras_changed/zones_changed/detection_rules_changed (+ %s)", _REQUEST)

    def _on_notify(self, _channel: str, _payload: str) -> None:
        self._tasks.spawn(self._publish())

    async def _on_request(self, msg) -> None:
        if not msg.reply:
            return
        try:
            await self._nc.publish(msg.reply, await self._build())
        except Exception:
            log.exception("roster bridge: reply failed")

    async def _build(self) -> bytes:
        # Effective per-camera detector classes = the global enabled set, minus
        # any per-camera DISABLE (enabled=False), plus any per-camera ENABLE
        # (enabled=True). A per-camera row with enabled=NULL means "inherit the
        # global decision" (it typically exists only to override min_confidence)
        # — it must NOT be treated as a disable. This mirrors the detector's own
        # `DetectionRules.effective()` NULL-inherits semantics; keeping them in
        # sync is what stops the roster from LYING to DIDA about person/car on a
        # camera that merely tuned that class's confidence.
        global_classes = {
            r["class_name"]
            for r in await self._pool.fetch(
                "SELECT class_name FROM detector_global_rules WHERE enabled"
            )
        }
        overrides: dict[str, dict[str, bool | None]] = {}
        for r in await self._pool.fetch(
            "SELECT camera_id, class_name, enabled FROM camera_detection_rules"
        ):
            overrides.setdefault(str(r["camera_id"]), {})[r["class_name"]] = r["enabled"]

        # Scene regions in their own pass — joining them alongside zones would
        # multiply the rows. Each region DECLARES its state vocabulary so a
        # consumer can validate `scenes` per region instead of against a fixed
        # enum (a gate is open/closed, a parking spot present/empty, and the
        # labels are operator-defined at region creation).
        scenes_by_cam: dict[str, list[dict]] = {}
        for r in await self._pool.fetch(
            "SELECT id, camera_id, name, states, place FROM scene_regions"
            " WHERE enabled ORDER BY name"
        ):
            scenes_by_cam.setdefault(str(r["camera_id"]), []).append(
                # `id` is the identity, `name` is display — same rule as the
                # camera above. Without it a rename churned the consumer's
                # entity (delete+add), losing floor-plan placement and breaking
                # automation references; the state snapshot keys by this id.
                #
                # `place` is the OTHER half of the state message. `scenes` is
                # keyed by this id, `parked` by the place, and nothing else
                # published says the two are the same spot — a consumer could
                # only match the display name against the place key and hope,
                # which is true here by coincidence and breaks the day a region
                # is renamed to something readable. It is also how two regions
                # (Shed's view and West's) are known to be one place. `null`
                # for a region that stands for itself, published rather than
                # omitted: "watches no place" is a statement, not a gap.
                {"id": str(r["id"]), "name": r["name"],
                 "states": list(r["states"] or []), "place": r["place"]}
            )

        rows = await self._pool.fetch(
            """
            SELECT c.id AS camera_id, c.slug AS camera_slug, c.name AS camera_name,
                   c.enabled AS camera_enabled, c.doorbell AS camera_doorbell,
                   z.id AS zone_id, z.name AS zone_name, z.kind AS zone_kind,
                   z.enabled AS zone_enabled, z.rules
            FROM cameras c
            LEFT JOIN zones z ON z.camera_id = c.id
            ORDER BY c.slug, z.name
            """
        )
        cams: dict[str, dict] = {}
        for r in rows:
            slug_ = r["camera_slug"]
            if slug_ not in cams:
                ov = overrides.get(str(r["camera_id"]), {})
                # `is True`/`is False`, NOT truthiness: NULL (inherit) must
                # neither add nor subtract — the whole bug was `if not v`
                # subtracting NULL-override classes.
                detect = (global_classes | {k for k, v in ov.items() if v is True}) - {
                    k for k, v in ov.items() if v is False
                }
                cams[slug_] = {
                    # `id` is the camera's API identity (events/recordings queries) —
                    # DIDA's proxy needs it; slugs stay the NATS/go2rtc identity.
                    "id": str(r["camera_id"]),
                    "slug": slug_, "name": r["camera_name"], "enabled": r["camera_enabled"],
                    # Doorbell presses ride their own subject (baba.bell.<uuid>)
                    # because a press is an EVENT, not state — this flag is how a
                    # consumer knows the camera has one at all.
                    "doorbell": bool(r["camera_doorbell"]),
                    "detect_classes": sorted(detect),
                    "scenes": scenes_by_cam.get(str(r["camera_id"]), []),
                    "zones": [],
                }
            if r["zone_name"] is None:
                continue  # camera with no zones
            rules = r["rules"]
            rules = json.loads(rules) if isinstance(rules, str) else (rules or {})
            classes = sorted((rules.get("enabled_classes") or {}).keys())
            cams[slug_]["zones"].append({
                # Identity, not the name — a renamed zone must keep its entity.
                "id": str(r["zone_id"]),
                "name": r["zone_name"], "kind": r["zone_kind"],
                "enabled": r["zone_enabled"], "classes": classes,
            })
        return json.dumps({"cameras": list(cams.values())}).encode()

    async def _publish(self) -> None:
        try:
            payload = await self._build()
            await self._nc.publish(_SUBJECT, payload)
            log.info("roster published")
        except Exception:
            log.exception("roster bridge: publish failed")

    async def stop(self) -> None:
        if self._listener is not None:
            await self._listener.stop()
        await self._tasks.stop()
