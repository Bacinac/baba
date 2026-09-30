"""What the tracker publishes for what it is fed, driven end to end through `run`.

NATS, Postgres and the shared-memory frame ring are replaced at the module's
boundary; Norfair, the motion state machine, the birth gate, the anchor hold
and the activity verdict are the real ones.
"""

import asyncio
import contextlib
import threading

import baba_tracker.__main__ as tm
import msgspec
import numpy as np
import pytest
from baba_core.wire import (
    ActivityMessage,
    DetectionsMessage,
    DetectionWire,
    TracksMessage,
)
from baba_tracker.config import TrackerConfig

CAM = "porch"
W, H = 1920, 1080
STEP_NS = 500_000_000
T0 = 1_750_000_000_000_000_000


class Bus:
    def __init__(self) -> None:
        self.route = None
        self.subscribed = asyncio.Event()
        self.tracks: list[TracksMessage] = []
        self.activity: list[ActivityMessage] = []
        self.is_closed = False

    async def subscribe(self, subject, cb):
        self.route = cb
        self.subscribed.set()

    async def publish(self, subject, data):
        if subject.startswith("baba.tracks."):
            self.tracks.append(msgspec.msgpack.decode(data, type=TracksMessage))
        elif subject.startswith("baba.activity."):
            self.activity.append(msgspec.msgpack.decode(data, type=ActivityMessage))

    async def drain(self):
        self.is_closed = True


class Cameras:
    live: Cameras

    def __init__(self, dsn, on_change, tunables=None) -> None:
        self.on_change = on_change
        self.lost: float | None = None
        self.disabled: set[str] = set()
        Cameras.live = self

    async def start(self):
        pass

    async def publish_defaults(self, tunables):
        pass

    async def stop(self):
        pass

    def stillness_ratio(self, slug, default):
        return default

    def park_ms(self, slug, default):
        return default

    def lost_seconds(self, slug, default):
        return default if self.lost is None else self.lost

    def reid_lost_seconds(self, slug, default):
        return default

    def disabled_slugs(self):
        return set(self.disabled)


class NoSpots:
    def __init__(self, dsn, tunables) -> None:
        pass

    async def start(self):
        raise OSError("no database in this test")


class LeftQuarterIgnored:
    def __init__(self, dsn) -> None:
        pass

    async def start(self):
        pass

    async def stop(self):
        pass

    def ignored(self, cam, box):
        return box[2] <= 0.25

    def in_parking(self, cam, box):
        return False


class NoIdentities:
    enabled = False

    def __init__(self, dsn, tunables, *, available) -> None:
        pass

    async def refresh_loop(self):
        await asyncio.Event().wait()

    def forget_camera(self, cam):
        pass


class OneLook:
    """Every crop looks the same, so appearance always agrees."""

    def __init__(self, path) -> None:
        pass

    def embed(self, crops):
        v = np.full((len(crops), 512), 1.0 / np.sqrt(512), dtype=np.float32)
        return v


class Frame:
    pixel_format = "rgb"
    width, height = W, H
    pixels = np.full((H, W, 3), 90, dtype=np.uint8)


class Ring:
    def __init__(self, slug) -> None:
        pass

    def get_by_sequence(self, seq):
        return Frame()

    def detach(self):
        pass


@pytest.fixture
def world(monkeypatch, tmp_path):
    model = tmp_path / "osnet.onnx"
    model.write_bytes(b"")
    for k, v in {
        "BABA_TRACKER_REID_MODEL": str(model),
        "BABA_TRACKER_STATIC_WINDOW_MS": "2000",
        "BABA_TRACKER_PARK_THRESHOLD_MS": "5000",
        "BABA_REQUIRE_REID": "0",
    }.items():
        monkeypatch.setenv(k, v)
    bus = Bus()

    async def connect(url, name):
        return bus

    monkeypatch.setattr(tm, "nats_connect", connect)
    monkeypatch.setattr(tm, "CameraSettings", Cameras)
    monkeypatch.setattr(tm, "PhantomSpots", NoSpots)
    monkeypatch.setattr(tm, "ZoneMasks", LeftQuarterIgnored)
    monkeypatch.setattr(tm, "IdentityStamp", NoIdentities)
    monkeypatch.setattr(tm, "OSNetOnnxBackend", OneLook)
    monkeypatch.setattr(tm, "FrameRingReader", Ring)
    monkeypatch.setattr(tm.HealthMarker, "touch", lambda self: None)
    return bus


class Feed:
    def __init__(self, bus: Bus) -> None:
        self.bus = bus
        self.seq = 0

    def wire(self, *boxes, cam=CAM) -> bytes:
        self.seq += 1
        dets = [
            DetectionWire(x1=x, y1=y, x2=x + 100, y2=y + 250, class_id=c, class_name=n, confidence=0.9)
            for x, y, c, n in boxes
        ]
        msg = DetectionsMessage(
            camera_id=cam, sequence=self.seq, timestamp_ns=T0 + self.seq * STEP_NS,
            frame_width=W, frame_height=H, detections=dets,
        )
        return msgspec.msgpack.encode(msg)

    async def send(self, *boxes, cam=CAM) -> None:
        before = len(self.bus.tracks)
        await self.bus.route(type("Msg", (), {"data": self.wire(*boxes, cam=cam)}))
        await settled(lambda: len(self.bus.tracks) > before)


async def settled(done) -> None:
    for _ in range(5000):
        if done():
            return
        await asyncio.sleep(0.001)
    raise AssertionError("the tracker never published")


def person(x, y=500):
    return (x, y, 0, "person")


def drive(bus: Bus, script) -> None:
    async def main():
        service = asyncio.create_task(tm.run(TrackerConfig.from_env()))
        await bus.subscribed.wait()
        try:
            await script(Feed(bus))
        finally:
            service.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await service

    asyncio.run(main())


def walk_in_and_sit(feed):
    async def steps():
        for i in range(10):
            await feed.send(person(800 + 30 * i))
        for _ in range(20):
            await feed.send(person(1070))

    return steps()


def by_seq(bus: Bus) -> dict[int, tuple[TracksMessage, ActivityMessage]]:
    return {m.sequence: (m, a) for m, a in zip(bus.tracks, bus.activity, strict=True)}


def test_a_walker_settles_parks_and_is_held_while_the_detector_is_blind(world):
    async def script(feed):
        await walk_in_and_sit(feed)
        for _ in range(240):
            await feed.send()

    drive(world, script)
    out = by_seq(world)
    assert sorted(out) == list(range(1, 271))
    assert out[1][0].tracks == [] and out[1][1].active
    carried = [s for s, (m, _) in out.items() if [t.track_id for t in m.tracks] == [1]]
    assert carried == list(range(2, 229))
    assert all(out[s][0].tracks == [] for s in range(229, 271))
    state = {s: out[s][0].tracks[0].motion_state for s in carried}
    assert {s for s, v in state.items() if v == "active"} == set(range(2, 13))
    assert {s for s, v in state.items() if v == "stationary"} == set(range(13, 23))
    assert {s for s, v in state.items() if v == "parked"} == set(range(23, 229))
    assert [s for s, (_, a) in out.items() if a.active] == list(range(1, 23))
    assert len({m.epoch for m, _ in out.values()}) == 1


def test_a_subject_back_in_view_continues_the_held_track(world):
    async def script(feed):
        await walk_in_and_sit(feed)
        for _ in range(60):
            await feed.send()
        for _ in range(6):
            await feed.send(person(1070))

    drive(world, script)
    out = by_seq(world)
    ids = {s: [(t.track_id, t.continues_track_id) for t in out[s][0].tracks] for s in range(90, 97)}
    assert ids[90] == ids[91] == [(1, 0)]
    assert ids[92] == ids[93] == ids[94] == [(2, 0), (1, 0)]
    assert ids[95] == [(2, 1)]
    assert ids[96] == [(2, 0)]


def test_ignored_ground_and_unlisted_classes_never_become_tracks(world):
    async def script(feed):
        for i in range(6):
            await feed.send(person(800 + 30 * i), person(100), (400, 300, 16, "dog"))

    drive(world, script)
    assert {(t.track_id, t.class_name, t.x1) for m in world.tracks for t in m.tracks} == {
        (1, "person", 800 + 30 * i) for i in range(1, 6)
    }


def test_a_changed_lost_window_restarts_the_cameras_ids_and_their_history(world):
    async def script(feed):
        await walk_in_and_sit(feed)
        Cameras.live.lost = 5.0
        for _ in range(3):
            await feed.send(person(1070))

    drive(world, script)
    out = by_seq(world)
    before, after = out[30][0], out[31][0]
    assert [(t.track_id, t.motion_state) for t in before.tracks] == [(1, "parked")]
    assert after.epoch == before.epoch + 1 and after.tracks == []
    assert [(t.track_id, t.motion_state) for t in out[32][0].tracks] == [(1, "active")]
    assert out[31][1].active


def test_a_disabled_camera_loses_its_state_and_no_other_camera_does(world):
    async def script(feed):
        for i in range(3):
            await feed.send(person(800 + 30 * i))
            await feed.send(person(800 + 30 * i), cam="yard")
        Cameras.live.disabled = {CAM}
        Cameras.live.on_change()
        Cameras.live.disabled = set()
        for i in range(3, 5):
            await feed.send(person(800 + 30 * i))
            await feed.send(person(800 + 30 * i), cam="yard")

    drive(world, script)
    porch = [m for m in world.tracks if m.camera_id == CAM]
    yard = [m for m in world.tracks if m.camera_id == "yard"]
    assert len({m.epoch for m in yard}) == 1
    assert porch[3].epoch == porch[2].epoch + 1
    assert [len(m.tracks) for m in porch] == [0, 1, 1, 0, 1]
    assert [len(m.tracks) for m in yard] == [0, 1, 1, 1, 1]


def test_a_backed_up_camera_drops_its_stalest_frames(world):
    async def script(feed):
        for i in range(70):
            await feed.bus.route(type("Msg", (), {"data": feed.wire(person(800 + i))}))
        await settled(lambda: feed.bus.tracks and feed.bus.tracks[-1].sequence == 70)

    drive(world, script)
    assert [m.sequence for m in world.tracks] == list(range(7, 71))


def test_osnet_runs_off_the_loop_so_other_cameras_keep_flowing(world, monkeypatch):
    release = threading.Event()

    class SlowLook(OneLook):
        def embed(self, crops):
            release.wait(5)
            return super().embed(crops)

    monkeypatch.setattr(tm, "OSNetOnnxBackend", SlowLook)

    async def script(feed):
        await feed.bus.route(type("Msg", (), {"data": feed.wire(person(800))}))
        await feed.send(cam="yard")
        release.set()
        await settled(lambda: len(feed.bus.tracks) == 2)

    drive(world, script)
    assert [m.camera_id for m in world.tracks] == ["yard", CAM]
