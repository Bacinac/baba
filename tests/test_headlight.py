"""The rule that decides a headlight is drowning the plate zone.

Every number here is a measurement off west, 28.08 — the quiet night, the two
arrivals, the departure and the daytime samples — so a change that breaks one of
these breaks it against footage, not against an opinion.
"""

import asyncio
import json

import numpy as np
from baba_state_evaluator.headlight import (
    _HOLD_S,
    HeadlightWatcher,
    decide,
    zone_luma,
)


class Frame:
    def __init__(self, pixels, width, height):
        self.pixels = pixels
        self.width = width
        self.height = height


def test_the_measurement_is_of_the_luma_plane_only():
    """NV12 keeps interleaved chroma under the picture. Averaging into it reads
    a number that has nothing to do with brightness — and around 128, which is
    exactly the range a bloom is judged against."""
    h, w = 40, 40
    buf = np.full((h + h // 2, w), 200, dtype=np.uint8)
    buf[:h] = 50                       # the picture is dark
    frame = Frame(buf, w, h)
    assert zone_luma(frame, (0.0, 0.0, 1.0, 1.0)) == 50.0


def test_a_zone_outside_the_frame_measures_nothing_rather_than_zero():
    frame = Frame(np.full((60, 40), 50, dtype=np.uint8), 40, 40)
    assert zone_luma(frame, (0.5, 0.5, 0.2, 0.2)) is None


def test_a_night_arrival_fires_and_a_departure_does_not():
    """Measured: a quiet night sits at 53 and moves by 4%; the two arrivals
    peaked at 176 and 220; the departure at 90. Tail lights do not blind the
    camera and the plate faces away, so there is nothing to fix."""
    assert decide(176.0, 53.1, is_night=True) is True
    assert decide(220.0, 53.5, is_night=True) is True
    assert decide(90.0, 53.1, is_night=True) is False
    assert decide(54.4, 53.1, is_night=True) is False


def test_daylight_never_fires_however_bright_the_zone_gets():
    """An afternoon reaches the same numbers an arrival does — 188 measured at
    14:00 — so the zone's own brightness cannot separate them. Swapping a camera
    to IR by day would ruin the picture to fix nothing."""
    assert decide(250.0, 115.0, is_night=False) is False
    assert decide(188.0, 115.0, is_night=False) is False
    # And a dark-looking zone by day is still day: the camera says so, not the
    # rectangle.
    assert decide(220.0, 53.1, is_night=False) is False


def test_a_floodlit_yard_is_still_night():
    """The gate used to be the zone baseline under a fixed 80, and west lives at
    80-87 once its floodlight comes on. On the evening of 02.09 the rule stood
    down at 21:15, armed at 21:19, stood down at 21:21, armed at 21:51 — across
    the whole arrival window. Shed's zone sits at 98 all night and was never
    armed at all. A doubling over that baseline is still a headlight."""
    assert decide(231.8, 87.0, is_night=True) is True
    assert decide(210.0, 98.0, is_night=True) is True
    # Not everything above the baseline is: the ratio still has to be reached.
    assert decide(150.0, 87.0, is_night=True) is False


def test_nothing_fires_before_there_is_a_baseline_to_judge_against():
    assert decide(220.0, None, is_night=True) is False


def test_the_filter_is_mechanical_so_a_trigger_in_cooldown_is_ignored():
    assert decide(220.0, 53.1, is_night=True, in_cooldown=True) is False


class FakePool:
    def __init__(self, rows):
        self._rows = rows

    async def fetch(self, _sql, *_args):
        return self._rows


class FakeNats:
    def __init__(self):
        self.asked = []

    async def request(self, _subject, data, **_):
        body = json.loads(data)
        self.asked.append(body)

        class _Msg:
            data = json.dumps({"applied": True, "error": None}).encode()

        return _Msg()


def _watcher(luma_per_call, nats):
    row = {
        "id": "cam", "slug": "west", "light_condition": "ir",
        "polygons": [[[0, 0], [1, 0], [1, 1], [0, 1]]],
    }
    seq = iter(luma_per_call)

    class _Reader:
        def get_latest(self):
            v = next(seq)
            return Frame(np.full((6, 4), v, dtype=np.uint8), 4, 4)

    return HeadlightWatcher(FakePool([row]), nats, lambda _slug: _Reader())


def test_the_bloom_cannot_raise_the_baseline_that_would_have_caught_it():
    """While the camera is held, every frame is of a picture we changed. Folding
    those in teaches the watcher that the override is the normal state of the
    yard — and the next arrival passes unnoticed."""
    nats = FakeNats()
    quiet = [53.0] * 12
    w = _watcher(quiet + [176.0] + [90.0] * 3, nats)

    async def _run():
        for _ in range(12):
            await w._tick()
        await w._tick()                       # the headlight
        for _ in range(3):
            await w._tick()                   # held: three bright frames

    asyncio.run(_run())
    assert [a["release"] for a in nats.asked] == [False], "held once, asked once"
    assert w._history["west"] == quiet, "nothing measured while the camera is ours"


def test_the_camera_is_handed_back_when_the_hold_runs_out():
    nats = FakeNats()
    w = _watcher([53.0] * 12 + [176.0, 53.0], nats)

    async def _run():
        for _ in range(13):
            await w._tick()
        w._held_until["west"] -= _HOLD_S + 1   # the hold has run out
        await w._tick()

    asyncio.run(_run())
    assert [a["release"] for a in nats.asked] == [False, True]
    assert "west" not in w._held_until
