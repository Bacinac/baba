"""What one tracks message does to the event-manager, asked of `_observe` in real SQL.

A tracks message opens or carries a visit, files zone and parking transitions,
bakes the visit's thumbnail and publishes the camera's snapshot. Every branch
is driven here against the migrated schema, with the live identity matcher,
the thumbnail baker, the presence registry and the snapshot publisher standing
in at the edges.
"""

import asyncio
import dataclasses
import json
from uuid import uuid4

import asyncpg
from baba_core.wire import TracksMessage, TrackWire
from baba_event_manager.__main__ import EventManager
from baba_event_manager.config import EventManagerConfig
from baba_event_manager.zones import ZonesResolver

S = 1_000_000_000
T0 = 1_750_000_000 * S
EPOCH = 1_750_000_000_000_000_000
LEFT = [[0.0, 0.0], [0.5, 0.0], [0.5, 1.0], [0.0, 1.0]]
RIGHT = [[0.5, 0.0], [1.0, 0.0], [1.0, 1.0], [0.5, 1.0]]
OUT, IN = 200.0, 700.0


class Live:
    def __init__(self) -> None:
        self.transfers: list[tuple] = []
        self.gid = uuid4()

    def transfer(self, camera_id, old_tid, new_tid):
        self.transfers.append((camera_id, old_tid, new_tid))

    async def resolve(self, conn, camera_id, camera_uuid, track_ids):
        return {self.gid: ("Marko", "face")} if 1 in track_ids else {}


class Baker:
    def __init__(self) -> None:
        self.baked: list[tuple] = []

    def bake(self, slug, sequence, box):
        self.baked.append((sequence, box))
        return b"jpeg%d" % sequence


class Presence:
    def __init__(self) -> None:
        self.observed: list[dict] = []

    def since_of(self, camera_id, gid):
        return None

    async def observe(self, camera_id, matched):
        self.observed.append(matched)


class Snapshot:
    def __init__(self) -> None:
        self.frames: list[dict] = []

    async def update_vision(self, camera_id, camera_name, tracks, zones_open, ts_ns, identities,
                            identity_sources, identity_since=None, object_identities=None,
                            object_identity_kinds=None):
        self.frames.append({
            "tracks": [t.track_id for t in tracks], "zones": zones_open, "identities": identities,
            "sources": identity_sources, "objects": object_identities, "kinds": object_identity_kinds,
        })


class World:
    def __init__(self, pool, em: EventManager) -> None:
        self.pool = pool
        self.em = em
        self.seq = 0
        self.zone: dict = {}
        self.finalized: list[tuple] = []

    async def build(self, zones) -> None:
        async with self.pool.acquire() as conn:
            cam = await conn.fetchval(
                "INSERT INTO cameras (name, slug, stream_url) VALUES ('gate', 'gate', 'rtsp://go2rtc/x') RETURNING id"
            )
            for name, kind, poly, rules in zones:
                self.zone[name] = await conn.fetchval(
                    "INSERT INTO zones (camera_id, name, kind, polygon, rules) "
                    "VALUES ($1, $2, $3, $4::jsonb, $5::jsonb) RETURNING id",
                    cam, name, kind, json.dumps(poly), json.dumps(rules or {}),
                )
            await self.em._resolver.refresh(conn)
            await self.em._zones.refresh(conn)

    async def tick(self, *tracks, at_s: float | None = None, epoch=EPOCH) -> None:
        self.seq += 1
        ts = T0 + int((self.seq if at_s is None else at_s) * S)
        await self.em._observe(TracksMessage(
            camera_id="gate", sequence=self.seq, timestamp_ns=ts, tracks=list(tracks),
            frame_width=1000, frame_height=1000, epoch=epoch,
        ))

    def rec(self, tid):
        return self.em._state["gate"].get(tid)

    async def events(self) -> list[tuple]:
        rows = await self.pool.fetch("SELECT kind, track_id, at, payload FROM events ORDER BY at, (payload->>'track_id')::int, kind")
        out = []
        for r in rows:
            p = json.loads(r["payload"])
            out.append((r["kind"], p.get("zone_name"), int(p["track_id"]), r["at"].timestamp() - T0 / S, r["track_id"]))
        return out


def track(tid, x=OUT, *, cls=(0, "person"), conf=0.8, motion="active", y=400.0, w=100.0, h=250.0, **kw):
    return TrackWire(track_id=tid, x1=x, y1=y, x2=x + w, y2=y + h, class_id=cls[0], class_name=cls[1],
                     confidence=conf, motion_state=motion, **kw)


CAR = (2, "car")
DOG = (16, "dog")


def run(pg, media, body, *, zones=(), dwell_ms=3000) -> None:
    config = dataclasses.replace(
        EventManagerConfig.from_env(),
        dsn=pg, media_path=str(media), go2rtc_url="http://127.0.0.1:9", go2rtc_auth=("baba", "test"), dwell_threshold_ms=dwell_ms,
    )

    async def main():
        pool = await asyncpg.create_pool(pg, min_size=1, max_size=4)
        em = EventManager(config)
        em._pool = pool
        em._zones = ZonesResolver()
        em._live_identity = Live()
        em._snapshot_baker = Baker()
        em._presence = Presence()
        em._snapshot = Snapshot()
        w = World(pool, em)

        async def finalize(cam, tid, rec):
            w.finalized.append((cam, tid, rec.db_track_id))

        em._finalize = finalize
        try:
            await w.build(zones)
            await body(w)
        finally:
            await em._thumbs.close()
            await pool.close()

    asyncio.run(main())


def test_a_walk_through_a_zone_files_enter_dwell_and_exit(pg, tmp_path):
    async def body(w):
        await w.tick(track(1, OUT))
        await w.tick(track(1, OUT))
        for _ in range(5):
            await w.tick(track(1, IN))
        await w.tick(track(1, OUT))
        db = w.rec(1).db_track_id
        assert await w.events() == [
            ("zone_enter", "porch", 1, 3.0, db),
            ("zone_dwell", "porch", 1, 6.0, db),
            ("zone_exit", "porch", 1, 8.0, db),
        ]
        porch = str(w.zone["porch"])
        assert [f["zones"][porch] for f in w.em._snapshot.frames] == [False] * 2 + [True] * 5 + [False]
        assert w.rec(1).touched_zone and w.rec(1).ever_enter_fired
        assert (w.rec(1).n_observations, w.rec(1).min_cx, w.rec(1).max_cx) == (8, 250.0, 750.0)
        assert sum(sum(g) for g in w.em._heatmap.drain().values()) == 8

    run(pg, tmp_path, body, zones=[("porch", "generic", RIGHT, None)])


def test_a_zone_rule_admits_only_its_class_its_confidence_and_after_its_dwell(pg, tmp_path):
    rules = {"enabled_classes": {"person": {"min_dwell_ms": 2000, "min_confidence": 0.5, "cooldown_s": 60}}}

    async def body(w):
        await w.tick(track(1, IN), track(2, IN, cls=CAR), track(3, IN, conf=0.4))
        await w.tick(track(1, IN), track(2, IN, cls=CAR), track(3, IN, conf=0.4))
        await w.tick(track(1, IN), track(2, IN, cls=CAR), track(3, IN, conf=0.4))
        await w.tick(track(1, IN))
        await w.tick(track(1, OUT))
        await w.tick(track(1, IN))
        await w.tick(track(1, IN))
        await w.tick(track(1, IN))
        await w.tick(track(4, IN))
        await w.tick(track(4, OUT))
        assert [e[:4] for e in await w.events()] == [
            ("zone_enter", "porch", 1, 3.0),
            ("zone_dwell", "porch", 1, 4.0),
            ("zone_exit", "porch", 1, 5.0),
        ]
        assert not w.rec(4).inside_zones and w.rec(4).touched_zone and not w.rec(4).ever_enter_fired

    run(pg, tmp_path, body, zones=[("porch", "generic", RIGHT, rules)])


def test_a_parking_zone_hears_only_a_moving_vehicle(pg, tmp_path):
    async def body(w):
        await w.tick(track(1, OUT, cls=CAR), track(2, IN, cls=CAR, motion="parked"),
                     track(3, OUT), track(4, OUT, cls=CAR))
        await w.tick(track(1, IN, cls=CAR, motion="parked"), track(2, IN, cls=CAR, motion="parked"),
                     track(3, IN, motion="parked"), track(4, IN, cls=CAR))
        assert [e[:3] for e in await w.events() if e[0].startswith("zone")] == [
            ("zone_enter", "park", 3),
            ("zone_enter", "park", 4),
        ]
        assert not w.rec(2).ever_active and not w.rec(2).inside_zones

    run(pg, tmp_path, body, zones=[("park", "parking", RIGHT, None)])


def test_a_vehicle_at_rest_files_parked_and_its_visit_is_due_a_departure_is_a_new_visit(pg, tmp_path):
    """A car born standing still — a static-clutter phantom, an IR re-birth —
    never arrived, so it files no object_parked; one that drives off after
    that is a departure all the same."""
    async def body(w):
        await w.tick(track(1, 300, cls=CAR))
        await w.tick(track(1, 320, cls=CAR, motion="stationary"))
        await w.tick(track(1, 320, cls=CAR, motion="parked"))
        assert w.rec(1).park_finalize_due and w.rec(1).came_to_rest
        first = w.rec(1).db_track_id
        w.rec(1).parked_finalized = True
        await w.tick(track(1, 340, cls=CAR))
        assert w.rec(1).db_track_id != first and not w.rec(1).parked_finalized
        await w.tick(track(2, 0, cls=CAR))
        await w.tick(track(2, 0, cls=CAR, motion="stationary"))
        await w.tick(track(3, 500, cls=CAR, motion="parked"))
        await w.tick(track(3, 500, cls=CAR, motion="parked"))
        await w.tick(track(3, 520, cls=CAR))
        await w.tick(track(3, 520, cls=CAR, motion="stationary"))
        await w.tick(track(4, 700, cls=CAR, motion="stationary"))
        await w.tick(track(4, 700, cls=CAR, motion="parked"))
        assert [e[:3] for e in await w.events()] == [
            ("object_parked", None, 1),
            ("object_unparked", None, 1),
            ("object_unparked", None, 3),
            ("object_parked", None, 3),
        ]
        assert not w.rec(2).park_finalize_due
        assert w.rec(3).park_finalize_due

    run(pg, tmp_path, body)


def test_a_continuation_carries_the_visit_and_folds_a_young_record(pg, tmp_path):
    async def body(w):
        await w.tick(track(1, IN, first_seen_ns=T0 - 5 * S))
        first = w.rec(1)
        assert first.first_seen_ns == T0 - 5 * S
        await w.tick(track(1, IN))
        await w.tick(track(2, IN, continues_track_id=1))
        assert w.rec(2) is first and w.rec(1) is None
        await w.tick(track(3, IN, first_seen_ns=T0 - 3600 * S))
        young = w.rec(3)
        assert young.first_seen_ns == T0 + 4 * S
        await w.tick(track(3, IN, continues_track_id=2, conf=0.95))
        assert w.rec(3) is first and w.rec(2) is None
        assert (first.n_observations, first.max_confidence) == (5, 0.95)
        assert w.em._live_identity.transfers == [("gate", 1, 2), ("gate", 2, 3)]
        visit = first.db_track_id
        assert await w.events() == [
            ("zone_enter", "porch", 1, 1.0, visit),
            ("zone_enter", "porch", 3, 4.0, young.db_track_id),
            ("zone_dwell", "porch", 3, 5.0, visit),
        ]

    run(pg, tmp_path, body, zones=[("porch", "generic", RIGHT, None)])


def test_a_new_tracker_generation_retires_the_old_ones_records(pg, tmp_path):
    async def body(w):
        await w.tick(track(1, IN), track(2, IN, cls=CAR))
        old = w.rec(1).db_track_id
        w.rec(2).parked_finalized = True
        await w.tick(track(1, IN), epoch=EPOCH + 1)
        assert w.finalized == [("gate", 1, old)]
        assert w.rec(1).db_track_id != old and w.rec(2) is None
        await w.tick(track(1, IN), epoch=0)
        assert len(w.finalized) == 1

    run(pg, tmp_path, body)


def test_the_thumbnail_follows_the_clearest_active_view_and_never_teleports(pg, tmp_path):
    async def body(w):
        await w.tick(track(1, 300, conf=0.5))
        await w.tick(track(1, 310, conf=0.52))
        await w.tick(track(1, 320, conf=0.6))
        await w.tick(track(1, 700, conf=0.9))
        await w.tick(track(1, 710, conf=0.95, motion="stationary"))
        assert [s for s, _ in w.em._snapshot_baker.baked] == [1, 3]
        assert (w.rec(1).best_thumb_jpeg, w.rec(1).best_thumb_conf) == (b"jpeg3", 0.6)
        assert w.em._snapshot_baker.baked[0][1] == (0.3, 0.4, 0.4, 0.65)

    run(pg, tmp_path, body)


def test_the_snapshot_speaks_only_for_subjects_that_moved(pg, tmp_path):
    async def body(w):
        gid = str(uuid4())
        await w.tick(
            track(1, IN),
            track(2, IN, motion="parked"),
            track(5, IN, cls=DOG, identity_gid=gid, identity_name="Lumi"),
            track(6, IN, identity_gid=str(uuid4()), identity_name="Nobody"),
        )
        await w.tick()
        live = str(w.em._live_identity.gid)
        first, empty = w.em._snapshot.frames
        assert first["tracks"] == [1, 5, 6]
        assert first["zones"] == {str(w.zone["porch"]): True, str(w.zone["drive"]): False}
        assert (first["identities"], first["sources"]) == ({live: "Marko"}, {live: "face"})
        assert (first["objects"], first["kinds"]) == ({gid: "Lumi"}, {gid: "pet"})
        assert w.em._presence.observed == [{w.em._live_identity.gid: ("Marko", "face")}]
        assert empty["tracks"] == [] and not any(empty["zones"].values()) and empty["identities"] == {}

    run(pg, tmp_path, body, zones=[("porch", "generic", RIGHT, None), ("drive", "generic", LEFT, None)])
