"""What closing a track files, asked of `_finalize` itself in real SQL.

Finalize decides whether a track existed, what it was, whether the operator's
gates hide it, which of its samples speak for it, and who it is — and writes
all of it in one transaction. Every branch is exercised here against the
migrated schema, so that cutting the method into steps cannot quietly change
an answer.
"""

import asyncio
import dataclasses
import json
import time
from uuid import uuid4

import asyncpg
import pytest
from baba_core.face import FRONTALITY_MAX
from baba_event_manager.__main__ import EventManager
from baba_event_manager._state import TrackState
from baba_event_manager.config import EventManagerConfig
from baba_event_manager.zones import ZonesResolver

MODEL = "auraface"
PERSON, CAR, TRUCK, DOG = (0, "person"), (2, "car"), (7, "truck"), (16, "dog")
NAMES = dict((PERSON, CAR, TRUCK, DOG))
S = 1_000_000_000
LEFT = [[0.0, 0.0], [0.5, 0.0], [0.5, 1.0], [0.0, 1.0]]
RIGHT = [[0.5, 0.0], [1.0, 0.0], [1.0, 1.0], [0.5, 1.0]]


def vec(axis: int, dist: float = 0.0, towards: int | None = None) -> str:
    """A unit vector at cosine distance `dist` from the unit vector on `axis`."""
    v = [0.0] * 512
    v[axis] = 1.0 - dist
    if dist:
        v[axis + 1 if towards is None else towards] = (1.0 - (1.0 - dist) ** 2) ** 0.5
    return "[" + ",".join(map(repr, v)) + "]"


class _Live:
    def __init__(self, verdicts: dict) -> None:
        self._verdicts = verdicts

    def verdict(self, camera_id: str, local_track_id: int):
        return self._verdicts.get((camera_id, local_track_id))


class World:
    def __init__(self, pool, em: EventManager, media) -> None:
        self.pool = pool
        self.em = em
        self.media = media
        self.end_ns = time.time_ns() - 60 * S
        self.cam: dict = {}
        self.zone: dict = {}

    async def build(self) -> None:
        async with self.pool.acquire() as conn:
            for slug in ("yard", "gate"):
                self.cam[slug] = await conn.fetchval(
                    "INSERT INTO cameras (name, slug, stream_url) VALUES ($1, $1, 'rtsp://go2rtc/x') RETURNING id",
                    slug,
                )
            for name, poly in (("drive", LEFT), ("porch", RIGHT)):
                self.zone[name] = await conn.fetchval(
                    "INSERT INTO zones (camera_id, name, polygon) VALUES ($1, $2, $3::jsonb) RETURNING id",
                    self.cam["gate"], name, json.dumps(poly),
                )
            await self.em._resolver.refresh(conn)
            await self.em._zones.refresh(conn)

    def rec(self, cls=PERSON, *, life_s=10.0, n=10, travel=300.0, thumb=True, votes=None, **fields) -> TrackState:
        r = TrackState(
            first_seen_ns=self.end_ns - int(life_s * S),
            last_seen_ns=self.end_ns + 2 * S,
            last_evidence_ns=self.end_ns,
            class_id=cls[0],
            class_name=cls[1],
            max_confidence=0.9,
            n_observations=n,
            last_bbox=(100.0, 100.0, 200.0, 300.0),
            min_cx=150.0,
            max_cx=150.0 + travel,
            min_cy=200.0,
            max_cy=200.0,
        )
        r.best_thumb_jpeg = b"\xff\xd8thumb" if thumb else None
        r.db_track_id = uuid4()
        r.class_counts = dict(votes or {cls[0]: n})
        r.class_names = {cid: NAMES[cid] for cid in r.class_counts}
        for k, v in fields.items():
            setattr(r, k, v)
        return r

    async def sample(self, r: TrackState, *, cam="yard", local=1, at_s=-5.0, conf=0.8, body=None,
                     crop=None, face=None, face_crop=None, face_px=None, face_score=None,
                     realigned=None, frontality=None, at_rest=None, model=MODEL, track=None):
        return await self.pool.fetchval(
            """
            INSERT INTO track_embedding_samples
                (camera_id, local_track_id, track_id, captured_at, pts_ns, sequence, confidence,
                 bbox, crop_path, embedding, face_embedding, face_embedding_model, face_crop_path,
                 face_score, face_px, face_realigned, face_frontality, at_rest)
            VALUES ($1, $2, $3, to_timestamp($4::float8), 0, 0, $5, '{0,0,1,1}', $6,
                    $7::text::vector, $8::text::vector, $9, $10, $11, $12, $13, $14, $15)
            RETURNING id
            """,
            self.cam[cam], local, track, self.end_ns / S + at_s, conf, crop, body, face,
            model if face else None, face_crop, face_score, face_px, realigned, frontality, at_rest,
        )

    async def past_track(self, cls=PERSON, *, cam="yard", hours_ago=1.0, body=None, face=None,
                         face_px=None, gid=None, face_verified=False, face_model=MODEL):
        tid = uuid4()
        await self.pool.execute(
            """
            INSERT INTO tracks_all (id, camera_id, local_track_id, class_id, class_name, started_at,
                                    ended_at, n_observations, global_id, embedding, face_embedding,
                                    face_embedding_model, face_px, face_verified)
            VALUES ($1, $2, 999, $3, $4, now() - $5::float8 * interval '1 hour' - interval '1 minute',
                    now() - $5::float8 * interval '1 hour', 10, $6, $7::text::vector,
                    $8::text::vector, $9, $10, $11)
            """,
            tid, self.cam[cam], cls[0], cls[1], hours_ago, gid or tid, body, face,
            face_model if face else None, face_px, face_verified,
        )
        return tid

    async def identity(self, name, kind="person", *, face=None, body=None, face_model=MODEL):
        gid = uuid4()
        await self.pool.execute(
            "INSERT INTO identity_labels (global_id, name, kind) VALUES ($1, $2, $3)", gid, name, kind
        )
        if face or body:
            await self.pool.execute(
                """
                INSERT INTO identity_reference_photos
                    (global_id, photo_path, face_embedding, face_embedding_model, body_embedding)
                VALUES ($1, 'reference_photos/x.jpg', $2::text::vector, $3, $4::text::vector)
                """,
                gid, face, face_model if face else None, body,
            )
        return gid

    async def finalize(self, r: TrackState, cam="yard", local=1) -> None:
        await self.em._finalize(cam, local, r)

    async def filed(self, r: TrackState) -> dict | None:
        row = await self.pool.fetchrow(
            """
            SELECT t.*, embedding::text AS body_vec,
                   EXISTS (SELECT 1 FROM tracks v WHERE v.id = t.id) AS visible,
                   extract(epoch FROM retain_until - ended_at) / 86400 AS keep_days
              FROM tracks_all t WHERE t.id = $1
            """,
            r.db_track_id,
        )
        return dict(row) if row else None

    async def events(self) -> list[tuple]:
        rows = await self.pool.fetch("SELECT kind, track_id, at, payload FROM events ORDER BY at, kind")
        return [(x["kind"], x["track_id"], x["at"], json.loads(x["payload"])) for x in rows]

    async def audit(self) -> list[dict]:
        rows = await self.pool.fetch("SELECT op, payload FROM identity_audit ORDER BY at")
        return [dict(json.loads(x["payload"]), op=x["op"]) for x in rows]

    async def count(self, table: str) -> int:
        return await self.pool.fetchval(f"SELECT count(*) FROM {table}")  # noqa: S608

    def stat(self, name: str) -> int:
        return self.em._stats._counters.get(name, 0)


def _run(pg, media, body, live=None) -> None:
    config = dataclasses.replace(
        EventManagerConfig.from_env(),
        dsn=pg, media_path=str(media), go2rtc_url="http://127.0.0.1:9", go2rtc_auth=("baba", "test"),
        min_track_lifetime_ms=1500, min_observations=5, static_move_ratio=0.5,
        reid_window_days=7, face_embedding_model=MODEL,
        reid_face_cosine_threshold=0.5, reid_face_sample_min_score=0.3, reid_confident_face_score=0.6,
        reid_body_cluster_window_hours=24, reid_body_cluster_threshold=0.2,
        reid_face_anchor_window_hours=6, reid_face_anchor_body_threshold=0.32,
        reid_face_anchor_xcam_threshold=0.35, reid_face_anchor_margin=0.05,
        reid_body_reference_pet_threshold=0.3, reid_body_reference_margin=0.05,
    )

    async def main():
        pool = await asyncpg.create_pool(pg, min_size=1, max_size=4)
        em = EventManager(config)
        em._pool = pool
        em._zones = ZonesResolver()
        em._live_identity = _Live(live or {})
        try:
            w = World(pool, em, media)
            await w.build()
            await body(w)
        finally:
            await em._thumbs.close()
            await pool.close()

    asyncio.run(main())


def near(x: float):
    return pytest.approx(x, abs=1e-4)


def test_a_twitch_leaves_a_count_and_no_row(pg, tmp_path):
    async def body(w):
        await w.finalize(w.rec(life_s=1.0))
        await w.finalize(w.rec(n=3), local=2)
        assert await w.count("tracks_all") == 0
        assert await w.count("events") == 0
        assert w.stat("below_existence_floor") == 2

    _run(pg, tmp_path, body)


def test_an_unknown_camera_files_nothing(pg, tmp_path):
    async def body(w):
        await w.finalize(w.rec(), cam="nowhere")
        assert await w.count("tracks_all") == 0
        assert await w.count("events") == 0
        assert w.stat("finalize_calls") == 1

    _run(pg, tmp_path, body)


def test_a_plain_visit_is_filed_under_its_own_identity(pg, tmp_path):
    async def body(w):
        r = w.rec()
        await w.finalize(r)
        t = await w.filed(r)
        assert t["visible"] and t["suppressed_reason"] is None
        assert (t["class_id"], t["class_name"], t["local_track_id"], t["n_observations"]) == (0, "person", 1, 10)
        assert t["global_id"] == r.db_track_id
        assert t["identity_source"] == "body"
        assert t["face_verified"] is False
        assert t["keep_days"] == 30
        assert t["started_at"].timestamp() == near(r.first_seen_ns / S)
        assert t["ended_at"].timestamp() == near(r.last_evidence_ns / S)
        assert (t["ever_active"], t["came_to_rest"]) == (False, False)
        assert t["embedding"] is None and t["crop_path"] is None and t["face_px"] is None
        assert t["thumbnail_path"] == f"thumbnails/{r.db_track_id}.jpg"
        assert (tmp_path / t["thumbnail_path"]).read_bytes() == b"\xff\xd8thumb"
        assert await w.events() == [
            (
                "track_finalized", r.db_track_id, t["ended_at"],
                {
                    "class_id": 0, "class_name": "person", "duration_ms": 10000,
                    "n_observations": 10, "max_confidence": 0.9, "movement_px": 300.0,
                    "class_votes": {"person": 10},
                    "last_bbox": {"x1": 100.0, "y1": 100.0, "x2": 200.0, "y2": 300.0},
                },
            )
        ]
        assert await w.audit() == []
        assert (w.stat("reid_new"), w.stat("reid_match")) == (1, 0)

    _run(pg, tmp_path, body)


def test_without_a_thumbnail_the_row_says_so(pg, tmp_path):
    async def body(w):
        r = w.rec(thumb=False)
        await w.finalize(r)
        t = await w.filed(r)
        assert t["visible"] and t["thumbnail_path"] is None

    _run(pg, tmp_path, body)


def test_the_majority_vote_names_the_class(pg, tmp_path):
    async def body(w):
        r = w.rec(TRUCK, votes={2: 7, 7: 3})
        await w.finalize(r)
        t = await w.filed(r)
        assert (t["class_id"], t["class_name"]) == (2, "car")
        assert (r.class_id, r.class_name) == (2, "car")
        [(_, _, _, payload)] = await w.events()
        assert payload["class_votes"] == {"car": 7, "truck": 3}
        assert (payload["class_id"], payload["class_name"]) == (2, "car")

    _run(pg, tmp_path, body)


def test_zone_exits_close_only_the_zones_that_were_entered(pg, tmp_path):
    async def body(w):
        drive, porch = w.zone["drive"], w.zone["porch"]
        r = w.rec(
            inside_zones={drive: w.end_ns - 5 * S, porch: w.end_ns - 5 * S},
            enter_emitted={drive}, touched_zone=True, ever_enter_fired=True,
        )
        await w.finalize(r, cam="gate")
        events = await w.events()
        assert [(k, tid) for k, tid, _, _ in events] == [
            ("track_finalized", r.db_track_id), ("zone_exit", r.db_track_id),
        ]
        _, _, at, payload = events[1]
        assert at.timestamp() == near(r.last_seen_ns / S)
        assert payload == {
            "zone_id": str(drive), "zone_name": "drive", "zone_kind": "generic",
            "bbox": [100.0, 100.0, 200.0, 300.0], "class_name": None, "track_id": "1",
        }
        assert (r.inside_zones, r.enter_emitted, r.dwell_emitted) == ({}, set(), set())

    _run(pg, tmp_path, body)


def test_a_vehicle_that_touched_no_zone_is_kept_and_hidden(pg, tmp_path):
    async def body(w):
        r = w.rec(CAR, touched_zone=False)
        await w.finalize(r, cam="gate")
        t = await w.filed(r)
        assert not t["visible"] and t["suppressed_reason"] == "off-zone"
        assert t["keep_days"] == 30
        assert t["thumbnail_path"] is None and t["global_id"] is None and t["identity_source"] is None
        assert t["ended_at"].timestamp() == near(r.last_evidence_ns / S)
        assert await w.events() == []
        assert w.stat("tracks_suppressed") == 1

    _run(pg, tmp_path, body)


def test_a_vehicle_under_the_dwell_is_hidden_and_releases_nothing(pg, tmp_path):
    """Its zone_dwell set belongs to the zones it was still inside, and those
    are closed first — so by the time the gate asks, there is nothing to
    release and no finalize row is written."""

    async def body(w):
        drive = w.zone["drive"]
        r = w.rec(
            CAR, touched_zone=True, ever_enter_fired=False,
            inside_zones={drive: w.end_ns - 40 * S}, dwell_emitted={drive},
        )
        await w.finalize(r, cam="gate")
        t = await w.filed(r)
        assert not t["visible"] and t["suppressed_reason"] == "sub-dwell"
        assert await w.events() == []

    _run(pg, tmp_path, body)


def test_a_vehicle_that_came_to_rest_in_a_zone_is_filed(pg, tmp_path):
    async def body(w):
        r = w.rec(CAR, touched_zone=True, ever_enter_fired=False, parked_finalized=True)
        await w.finalize(r, cam="gate")
        assert (await w.filed(r))["visible"]

    _run(pg, tmp_path, body)


def test_a_person_passes_the_zone_gate_and_the_static_gate(pg, tmp_path):
    async def body(w):
        r = w.rec(PERSON, touched_zone=False, travel=20.0)
        await w.finalize(r, cam="gate")
        t = await w.filed(r)
        assert t["visible"] and t["suppressed_reason"] is None

    _run(pg, tmp_path, body)


def test_a_car_that_never_moved_is_hidden_as_static(pg, tmp_path):
    async def body(w):
        quiet = w.rec(CAR, travel=20.0)
        await w.finalize(quiet)
        entered = w.rec(CAR, travel=20.0, ever_enter_fired=True)
        await w.finalize(entered, local=2)
        for r in (quiet, entered):
            t = await w.filed(r)
            assert not t["visible"] and t["suppressed_reason"] == "static"
            assert t["keep_days"] == 30
        assert await w.events() == []
        assert w.stat("tracks_suppressed") == 2

    _run(pg, tmp_path, body)


def test_samples_in_the_window_are_claimed_and_the_best_speaks_for_the_track(pg, tmp_path):
    async def body(w):
        r = w.rec()
        best = await w.sample(r, conf=0.9, body=vec(10), crop="crops/best.jpg")
        low = await w.sample(r, at_s=-3, conf=0.5, body=vec(11), crop="crops/low.jpg")
        edge = await w.sample(r, at_s=1.5, conf=0.4, body=vec(12), crop="crops/edge.jpg")
        early = await w.sample(r, at_s=-13, conf=0.99, body=vec(13), crop="crops/early.jpg")
        other = await w.sample(r, local=2, conf=0.99, body=vec(14), crop="crops/other.jpg")
        owner = await w.past_track()
        owned = await w.sample(r, conf=0.99, body=vec(15), crop="crops/owned.jpg", track=owner)
        await w.finalize(r)
        claims = dict(await w.pool.fetch("SELECT id, track_id FROM track_embedding_samples"))
        assert {k for k, v in claims.items() if v == r.db_track_id} == {best, low, edge}
        assert claims[early] is None and claims[other] is None and claims[owned] == owner
        t = await w.filed(r)
        assert t["crop_path"] == "crops/best.jpg"
        assert t["body_vec"] == await w.pool.fetchval("SELECT $1::text::vector::text", vec(10))
        assert t["face_embedding"] is None and t["face_px"] is None
        assert t["global_id"] == r.db_track_id and t["identity_source"] == "body"
        assert w.stat("reid_new") == 1

    _run(pg, tmp_path, body)


def test_a_vehicle_speaks_through_a_moving_sample_a_person_through_its_best(pg, tmp_path):
    async def body(w):
        car = w.rec(CAR)
        await w.sample(car, conf=0.95, body=vec(10), crop="crops/beside.jpg", at_rest=True)
        await w.sample(car, at_s=-3, conf=0.6, body=vec(11), crop="crops/driving.jpg", at_rest=False)
        await w.finalize(car)
        still = w.rec(CAR)
        await w.sample(still, local=2, conf=0.9, body=vec(12), crop="crops/still.jpg", at_rest=True)
        await w.sample(still, local=2, at_s=-3, conf=0.5, body=vec(13), crop="crops/dim.jpg", at_rest=True)
        await w.finalize(still, local=2)
        person = w.rec(PERSON)
        await w.sample(person, local=3, conf=0.9, body=vec(14), crop="crops/seated.jpg", at_rest=True)
        await w.sample(person, local=3, at_s=-3, conf=0.5, body=vec(15), crop="crops/walking.jpg", at_rest=False)
        await w.finalize(person, local=3)
        assert (await w.filed(car))["crop_path"] == "crops/driving.jpg"
        assert (await w.filed(car))["body_vec"] == await w.pool.fetchval("SELECT $1::text::vector::text", vec(11))
        assert (await w.filed(still))["crop_path"] == "crops/still.jpg"
        assert (await w.filed(person))["crop_path"] == "crops/seated.jpg"

    _run(pg, tmp_path, body)


def test_the_face_that_speaks_is_the_one_that_survived_alignment(pg, tmp_path):
    front_ok, front_bad = FRONTALITY_MAX / 3, FRONTALITY_MAX * 2

    async def body(w):
        mixed = w.rec()
        await w.sample(mixed, conf=0.8, body=vec(10), crop="crops/ear-body.jpg", face=vec(50),
                       face_crop="face_crops/ear.jpg", face_px=150, face_score=0.99, realigned=False)
        await w.sample(mixed, conf=0.7, body=vec(11), crop="crops/side-body.jpg", face=vec(51),
                       face_crop="face_crops/side.jpg", face_px=100, face_score=0.9, realigned=True,
                       frontality=front_bad)
        await w.sample(mixed, conf=0.6, body=vec(12), crop="crops/front-body.jpg", face=vec(52),
                       face_crop="face_crops/front.jpg", face_px=80, face_score=0.7, realigned=True,
                       frontality=front_ok)
        await w.finalize(mixed)

        turned = w.rec()
        await w.sample(turned, local=2, conf=0.8, body=vec(20), face=vec(53),
                       face_crop="face_crops/turned.jpg", face_px=90, face_score=0.9, realigned=True,
                       frontality=front_bad)
        await w.sample(turned, local=2, conf=0.7, body=vec(21), face=vec(54),
                       face_crop="face_crops/ear2.jpg", face_px=150, face_score=0.99, realigned=False)
        await w.finalize(turned, local=2)

        ears = w.rec()
        await w.sample(ears, local=3, conf=0.8, body=vec(30), face=vec(55),
                       face_crop="face_crops/ear3.jpg", face_px=150, face_score=0.99, realigned=False)
        await w.finalize(ears, local=3)

        t = await w.filed(mixed)
        assert (t["crop_path"], t["face_crop_path"], t["face_px"]) == (
            "crops/ear-body.jpg", "face_crops/front.jpg", 80.0)
        t = await w.filed(turned)
        assert (t["face_crop_path"], t["face_px"]) == ("face_crops/turned.jpg", 90.0)
        t = await w.filed(ears)
        assert (t["face_crop_path"], t["face_px"]) == ("face_crops/ear3.jpg", 0.0)

    _run(pg, tmp_path, body)


def test_an_enrolled_face_names_the_track(pg, tmp_path):
    async def body(w):
        marko = await w.identity("Marko", face=vec(60))
        r = w.rec()
        await w.sample(r, body=vec(10), crop="crops/a.jpg", face=vec(60, 0.1, 61), face_crop="face_crops/a.jpg",
                       face_px=120, face_score=0.8, realigned=True, frontality=0.1)
        await w.finalize(r)
        t = await w.filed(r)
        assert t["global_id"] == marko
        assert t["identity_source"] == "face" and t["face_verified"] is True
        assert t["keep_days"] == 90
        assert await w.audit() == [{
            "op": "auto_match", "gid": str(marko), "track_id": str(r.db_track_id), "dist": 0.1,
            "threshold": 0.5, "source": "reference", "modality": "face",
        }]
        assert (w.stat("reid_match"), w.stat("reid_body_cluster")) == (1, 0)

    _run(pg, tmp_path, body)


def test_a_small_face_or_another_models_face_names_nobody(pg, tmp_path):
    async def body(w):
        await w.identity("Marko", face=vec(60))
        await w.identity("Ana", face=vec(70), face_model="other")
        small = w.rec()
        await w.sample(small, body=vec(10), face=vec(60, 0.1, 61), face_px=40, face_score=0.8,
                       realigned=True, frontality=0.1)
        await w.finalize(small)
        foreign = w.rec()
        await w.sample(foreign, local=2, body=vec(20), face=vec(70, 0.1, 71), face_px=120,
                       face_score=0.8, realigned=True, frontality=0.1)
        await w.finalize(foreign, local=2)
        for r in (small, foreign):
            t = await w.filed(r)
            assert t["global_id"] == r.db_track_id
            assert t["identity_source"] == "body" and t["face_verified"] is False
            assert t["keep_days"] == 30
        assert await w.audit() == []
        assert w.stat("reid_new") == 2

    _run(pg, tmp_path, body)


def test_a_face_only_speaks_in_the_active_models_space(pg, tmp_path):
    async def body(w):
        await w.identity("Ana", face=vec(70), face_model=None)
        await w.past_track(face=vec(80), face_px=120, body=vec(90), face_model=None)
        await w.past_track(face=vec(80), face_px=120, body=vec(91), face_model="other")

        r = w.rec()
        await w.sample(r, body=vec(10), face=vec(80, 0.1, 81), face_crop="face_crops/foreign.jpg",
                       face_px=150, face_score=0.99, realigned=True, frontality=0.1, model="other")
        await w.sample(r, body=vec(11), face=vec(80, 0.1, 82), face_crop="face_crops/ours.jpg",
                       face_px=90, face_score=0.8, realigned=True, frontality=0.1)
        await w.sample(r, body=vec(12), face=vec(70, 0.1, 71), face_px=120, face_score=0.9,
                       realigned=True, frontality=0.1, model="other")
        await w.finalize(r)

        only_foreign = w.rec()
        await w.sample(only_foreign, local=2, body=vec(20), face=vec(80, 0.1, 83), face_px=150,
                       face_score=0.99, realigned=True, frontality=0.1, model="other")
        await w.finalize(only_foreign, local=2)

        t = await w.filed(r)
        assert t["global_id"] == r.db_track_id and t["identity_source"] == "body"
        assert (t["face_crop_path"], t["face_px"], t["face_embedding_model"]) == (
            "face_crops/ours.jpg", 90.0, MODEL)
        t = await w.filed(only_foreign)
        assert (t["face_embedding"], t["face_px"], t["face_embedding_model"]) == (None, None, None)
        assert await w.audit() == []

    _run(pg, tmp_path, body)


def test_a_past_face_joins_its_anonymous_cluster_only_off_a_confident_crop(pg, tmp_path):
    async def body(w):
        past = await w.past_track(face=vec(70), face_px=120, body=vec(80))
        sure = w.rec()
        await w.sample(sure, body=vec(10), face=vec(70, 0.1, 71), face_px=120, face_score=0.8,
                       realigned=True, frontality=0.1)
        await w.finalize(sure)
        unsure = w.rec()
        await w.sample(unsure, local=2, body=vec(20), face=vec(70, 0.1, 72), face_px=120,
                       face_score=0.4, realigned=True, frontality=0.1)
        await w.finalize(unsure, local=2)

        t = await w.filed(sure)
        assert t["global_id"] == past
        assert t["identity_source"] == "face" and t["face_verified"] is True and t["keep_days"] == 30
        t = await w.filed(unsure)
        assert t["global_id"] == unsure.db_track_id
        assert t["identity_source"] == "body" and t["face_verified"] is False
        assert await w.audit() == [{
            "op": "auto_match", "gid": str(past), "track_id": str(sure.db_track_id), "dist": 0.1,
            "threshold": 0.5, "source": "track", "modality": "face",
        }]

    _run(pg, tmp_path, body)


def test_a_live_verdict_is_adopted_when_finalize_finds_no_face(pg, tmp_path):
    marko = uuid4()
    live = {("yard", 1): (marko, "Marko", "face"), ("yard", 2): (marko, "Marko", "face"),
            ("yard", 3): (marko, "Marko", "body")}

    async def body(w):
        await w.pool.execute(
            "INSERT INTO identity_labels (global_id, name, kind) VALUES ($1, 'Marko', 'person')", marko
        )
        by_face = w.rec()
        await w.sample(by_face, body=vec(10), face=vec(90), face_px=120, face_score=0.8,
                       realigned=True, frontality=0.1)
        await w.finalize(by_face)
        car = w.rec(CAR)
        await w.finalize(car, local=2)
        by_body = w.rec()
        await w.finalize(by_body, local=3)

        t = await w.filed(by_face)
        assert t["global_id"] == marko
        assert t["identity_source"] == "body" and t["face_verified"] is True and t["keep_days"] == 60
        t = await w.filed(car)
        assert t["global_id"] == car.db_track_id and t["keep_days"] == 30
        t = await w.filed(by_body)
        assert t["global_id"] == marko
        assert t["identity_source"] == "body" and t["face_verified"] is False and t["keep_days"] == 60
        assert await w.audit() == []
        assert (w.stat("reid_match"), w.stat("reid_body_cluster"), w.stat("reid_new")) == (2, 2, 1)

    _run(pg, tmp_path, body, live=live)


def test_an_enrolled_pet_is_named_by_its_body_and_a_person_never_is(pg, tmp_path):
    async def body(w):
        lumi = await w.identity("Lumi", "pet", body=vec(100))
        dog = w.rec(DOG)
        await w.sample(dog, body=vec(100, 0.1, 101))
        await w.finalize(dog)
        person = w.rec()
        await w.sample(person, local=2, body=vec(100, 0.05, 102))
        await w.finalize(person, local=2)

        t = await w.filed(dog)
        assert t["global_id"] == lumi and t["identity_source"] == "body" and t["keep_days"] == 90
        t = await w.filed(person)
        assert t["global_id"] == person.db_track_id
        assert await w.audit() == []
        assert (w.stat("reid_match"), w.stat("reid_body_cluster"), w.stat("reid_new")) == (1, 1, 1)

    _run(pg, tmp_path, body)


def test_the_same_day_body_cluster_folds_an_anonymous_repeat_on_its_own_camera(pg, tmp_path):
    async def body(w):
        past = await w.past_track(body=vec(110))
        here = w.rec()
        await w.sample(here, body=vec(110, 0.1, 111))
        await w.finalize(here)
        there = w.rec()
        await w.sample(there, cam="gate", body=vec(110, 0.05, 112))
        await w.finalize(there, cam="gate")

        t = await w.filed(here)
        assert t["global_id"] == past and t["identity_source"] == "body" and t["keep_days"] == 30
        assert (await w.filed(there))["global_id"] == there.db_track_id
        assert await w.audit() == [{
            "op": "auto_match", "gid": str(past), "track_id": str(here.db_track_id), "dist": 0.1,
            "threshold": 0.2, "source": "body-cluster", "modality": "body",
        }]
        assert (w.stat("reid_match"), w.stat("reid_body_cluster"), w.stat("reid_new")) == (1, 1, 1)

    _run(pg, tmp_path, body)


def test_a_body_close_to_a_face_verified_named_track_takes_its_name(pg, tmp_path):
    async def body(w):
        marko = await w.identity("Marko")
        await w.past_track(body=vec(120), face_px=120, gid=marko, face_verified=True)
        same = w.rec()
        await w.sample(same, body=vec(120, 0.1, 121))
        await w.finalize(same)
        cross = w.rec()
        await w.sample(cross, cam="gate", body=vec(120, 0.33, 122))
        await w.finalize(cross, cam="gate")
        far = w.rec()
        await w.sample(far, local=3, body=vec(120, 0.4, 123))
        await w.finalize(far, local=3)

        for r in (same, cross):
            t = await w.filed(r)
            assert t["global_id"] == marko
            assert t["identity_source"] == "body" and t["face_verified"] is False and t["keep_days"] == 60
        assert (await w.filed(far))["global_id"] == far.db_track_id
        assert await w.audit() == [
            {"op": "auto_match", "gid": str(marko), "track_id": str(same.db_track_id), "dist": 0.1,
             "threshold": 0.32, "source": "face-anchor", "modality": "body-anchor"},
            {"op": "auto_match", "gid": str(marko), "track_id": str(cross.db_track_id), "dist": 0.33,
             "threshold": 0.35, "source": "face-anchor-xcam", "modality": "body-anchor"},
        ]
        assert (w.stat("reid_face_anchor_body"), w.stat("reid_new")) == (2, 1)

    _run(pg, tmp_path, body)
