"""Authoritative per-camera state snapshot for off-box consumers (DIDA).

BABA has the frames, the tracker and the zones — so BABA owns the truth AND
every bit of the hysteresis. The consumer is a pure mirror: it runs no
stale-state sweep, no zone off-delay, no PIR-style motion cooldown of its own.
That contract shapes everything here:

* FULL snapshot, never a delta. A dropped message self-corrects on the next
  publish — which is precisely why the consumer needs no sweep.
* Published only on CHANGE, so a quiet camera stays quiet on the bus.
* `person_count` / `object_class` MUST fall back to 0 / `none` the moment the
  scene empties. A mirror latches whatever we last said; a class left at its
  final value would hold an automation on forever.
* `motion` carries OUR cooldown (latched by a moving track, released
  _MOTION_COOLDOWN_S after the last one).
* `zones` carries OUR hysteresis: a still or seated person MUST keep its zone
  true or the consumer's automations flap. Two-threshold tracking is what makes
  that hold — the track now survives the confidence decay of sitting down, so
  the zone stays occupied without any keepalive trickery.
* UUID is identity — for the camera AND one level down. `zones` and `scenes`
  are keyed by ZONE/REGION UUID, never by name: keying by name meant a rename
  churned the consumer's entity (delete+add), losing floor-plan placement and
  breaking automation references. The roster maps id → name; here a rename is
  pure display and never touches a key.

Only fields we actually own are sent; what we omit, the consumer does not
mirror. `scenes` therefore appears only once a region has an evaluated state.
"""

from __future__ import annotations

import json
import logging

from baba_core import CANONICAL_NONE, canonical_class

log = logging.getLogger(__name__)

_SUBJECT = "baba.state.{camera_id}"

# How long `motion` stays latched after the last MOVING track. The consumer
# dropped its own PIR cooldown, so this is the only one left in the chain.
_MOTION_COOLDOWN_S = 60
_MOTION_COOLDOWN_NS = _MOTION_COOLDOWN_S * 1_000_000_000

# The snapshot carries ONE object_class, so when several kinds share the scene
# we publish the most consequential: a person present outranks a parked car.
_CLASS_PRIORITY = ("person", "vehicle", "animal", "other")


class StateSnapshotPublisher:
    """Builds and publishes `baba.state.<camera_uuid>`.

    Vision fields arrive per tracks message; scene fields arrive from the
    scene-status listener. Both merge into one per-camera body that is
    republished whenever it actually changes.
    """

    def __init__(self, nc) -> None:
        self._nc = nc
        # camera_uuid → current body (the thing we publish)
        self._cur: dict[str, dict] = {}
        # camera_uuid → last published serialization, for change detection
        self._last: dict[str, str] = {}
        # camera_uuid → ns deadline the motion latch releases at
        self._motion_until: dict[str, int] = {}

    def _motion(self, camera_id: str, moving_now: bool, ts_ns: int) -> bool:
        if moving_now:
            self._motion_until[camera_id] = ts_ns + _MOTION_COOLDOWN_NS
            return True
        return ts_ns < self._motion_until.get(camera_id, 0)

    async def update_vision(
        self,
        camera_id: str,
        camera_name: str,
        tracks,
        zones_open: dict[str, bool],
        ts_ns: int,
        identities: dict[str, str],
        identity_sources: dict[str, str],
        identity_since: dict[str, str] | None = None,
        object_identities: dict[str, str] | None = None,
        object_identity_kinds: dict[str, str] | None = None,
    ) -> None:
        """Fold one tracks message into the snapshot.

        `tracks` MUST already be filtered to REAL subjects (ever_active): a
        static-clutter phantom must never raise motion or report a person, and
        the caller applies the same gate the zone events use. `zones_open` is
        zone UUID → occupied for every zone the camera has, so a zone that
        emptied publishes `false` rather than vanishing (and a renamed one keeps
        its key). `identities` is identity UUID → name for the named people
        recognised on screen right now — by face OR by body anchored to a recent
        face confirmation (live_identity.py). `identity_sources` is the same
        keys → "face" | "body", so the consumer knows HOW each was recognised:
        a face confirmation is authoritative, a body anchor is a strong
        provisional guess a later face may correct.
        """
        classes = {canonical_class(t.class_id) for t in tracks}
        body = self._cur.setdefault(camera_id, {})
        body["camera_id"] = camera_id
        body["camera_name"] = camera_name
        body["motion"] = self._motion(
            camera_id,
            any((t.motion_state or "active").lower() == "active" for t in tracks),
            ts_ns,
        )
        body["person_count"] = sum(1 for t in tracks if canonical_class(t.class_id) == "person")
        body["object_class"] = next((c for c in _CLASS_PRIORITY if c in classes), CANONICAL_NONE)
        body["zones"] = zones_open
        # Named people recognised on screen RIGHT NOW. Keyed by identity UUID
        # (the stable entity key — a rename must not churn the consumer's
        # entity), but UNLIKE zones/scenes the NAME travels INLINE as the value
        # here: identities is {gid: name}, not {gid: true}. There is no people
        # section in baba.roster (it carries only cameras) — so the name comes
        # from THIS map, not the roster. Key by the gid, treat the name as a
        # display attribute that updates when it changes.
        #
        # Empty dict rather than omitted when nobody is recognised, so the
        # consumer can tell "looked, found nobody named" from "this field does
        # not exist" — the same contract as zones publishing `false` instead of
        # vanishing. person_count=1 with identities empty is a real, distinct
        # state: someone is here and we do not know who.
        body["identities"] = identities
        # Parallel map (same UUID keys) rather than nesting {name, source} into
        # `identities` — additive, so a consumer reading `identities[gid]` as a
        # name keeps working and only picks up the source once it opts in.
        # "face" = authoritative confirmation; "body" = a strong provisional
        # guess (face-anchored body chain) a later face may still override.
        body["identity_sources"] = identity_sources
        # Third parallel map, same UUID keys: when each present person's stay
        # BEGAN — the open presence episode's start, ISO. Additive for the same
        # reason identity_sources is; a person whose verdict is still inside
        # the sustain gate appears in `identities` without a since, and picks
        # one up once the stay earns its episode.
        body["identity_since"] = identity_since or {}
        # Named PET/VEHICLE subjects on screen — SEPARATE from `identities`
        # (people) so a consumer's people-logic never treats a car as a person.
        # `object_identities` is identity UUID → name; `object_identity_kinds`
        # is the same keys → "pet" | "vehicle". Source is always body-reference
        # (these have no face). Empty dict, not omitted, so "looked, none named"
        # differs from "field absent" — same contract as `identities`.
        body["object_identities"] = object_identities or {}
        body["object_identity_kinds"] = object_identity_kinds or {}
        await self._flush(camera_id)

    async def update_parked(self, assignments) -> None:
        """Fold the parked-vehicle join into every camera that views a place.

        `assignments` is the full current truth (baba_core.parked), so a camera
        whose place emptied gets `parked: {}` — published empty rather than
        omitted, the same contract as zones and identities: "looked, nothing
        parked" is a different statement from "this field does not exist".

        Keyed by PLACE (the stable cross-camera name), value carries the
        vehicle's display name and since when — inline like `identities`,
        because places have no roster entry to map a uuid through.
        """
        per_cam: dict[str, dict] = {}
        for pv in assignments:
            for cam_id in pv.camera_ids:
                per_cam.setdefault(cam_id, {})[pv.place] = {
                    "name": pv.name,
                    "since": pv.since.isoformat(),
                }
        for camera_id, body in self._cur.items():
            new = per_cam.get(camera_id, {})
            if body.get("parked") != new:
                body["parked"] = new
                await self._flush(camera_id)

    async def update_scene(
        self, camera_id: str, camera_name: str, region_id: str, state: str | None
    ) -> None:
        """Record one scene region's evaluated state, keyed by its UUID (the
        roster carries the name). `state=None` (region deleted or not yet
        evaluated) drops it from the snapshot rather than publishing a guess."""
        body = self._cur.setdefault(camera_id, {})
        body["camera_id"] = camera_id
        if camera_name:
            body["camera_name"] = camera_name
        scenes = body.setdefault("scenes", {})
        if state is None:
            scenes.pop(region_id, None)
            if not scenes:
                body.pop("scenes", None)
        else:
            scenes[region_id] = state
        await self._flush(camera_id)

    async def update_light(
        self, camera_id: str, camera_name: str, band: str | None
    ) -> None:
        """This camera's illumination band — the verdict, not the measurement.

        `dark` / `ir` / `dim` / `normal` / `bright`, straight off
        `cameras.light_condition`: the same value the setpoint schedule
        switches on, so a consumer and our own profile switch can never
        disagree about how dark it is. All of the hysteresis is already in it
        (classify_light's sticky bands plus the debounce on consecutive
        buckets), which is exactly what a mirror must not re-derive.

        The number behind it, `luma_avg`, deliberately does NOT travel: it moves
        every minute on a still scene, and a snapshot that publishes on change
        would turn every quiet camera into a once-a-minute talker to say
        nothing. The band is what anything downstream can act on.
        """
        body = self._cur.setdefault(camera_id, {})
        body["camera_id"] = camera_id
        if camera_name:
            body["camera_name"] = camera_name
        if band is None:
            body.pop("light_condition", None)
        else:
            body["light_condition"] = band
        await self._flush(camera_id)

    def forget(self, camera_id: str) -> None:
        """Camera removed/disabled — drop its state so a later re-add starts
        clean instead of diffing against a stale body."""
        self._cur.pop(camera_id, None)
        self._last.pop(camera_id, None)
        self._motion_until.pop(camera_id, None)

    async def _flush(self, camera_id: str) -> None:
        body = self._cur.get(camera_id)
        if body is None:
            return
        payload = json.dumps(body, sort_keys=True)
        if self._last.get(camera_id) == payload:
            return  # nothing changed — stay quiet
        self._last[camera_id] = payload
        try:
            await self._nc.publish(_SUBJECT.format(camera_id=camera_id), payload.encode())
        except Exception:
            # Best-effort, like the other bridges: the next change re-sends,
            # and a full snapshot means the consumer can't be left half-updated.
            log.exception("state snapshot: publish failed for %s", camera_id)

    async def republish_all(self) -> None:
        """Re-publish every camera's CURRENT body, bypassing the change dedupe.

        Publish-on-change plus a pure mirror has one blind spot: a consumer that
        connects (or restarts) while every camera is quiet never hears the
        stable state, so it runs on whatever it last had — indefinitely (a DIDA
        badge stuck after the adapter restarted mid-quiet). A low-rate heartbeat
        closes it: the mirror self-corrects within one interval of connecting.
        Bodies are unchanged snapshots, so this is idempotent on the consumer."""
        for camera_id, body in list(self._cur.items()):
            payload = json.dumps(body, sort_keys=True)
            self._last[camera_id] = payload
            try:
                await self._nc.publish(_SUBJECT.format(camera_id=camera_id), payload.encode())
            except Exception:
                log.exception("state snapshot: heartbeat publish failed for %s", camera_id)
