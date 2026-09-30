"""What the deterministic watcher may ask, and what it must not.

Three of its five kinds re-derived a judgement the pipeline had already made —
the tracker counts births and flips, the phantom-spot registry suppresses a
birth spot that never moves — and none of the three could act on what it
found. They opened 605 incidents here, every one closed unread, while the
registry held patio/car down at 1538 births with zero movers.

The fourth kind, active_pinned, was firing on a camera that had nowhere to
decay to: idle_fps = target_fps = 4 makes a duty of 1.0 the only possible
reading. That is why the rate contract is tested here beside the watcher.
"""

import json

import pytest
from baba_api.models import adaptive_rate_error
from baba_event_manager.telemetry import (
    DROP_SPIKE_MIN,
    WATCH_WINDOW_S,
    TelemetrySink,
)

LIVE_KINDS = {"active_pinned", "drop_spike"}
RETIRED_KINDS = {"birth_starved", "flip_spike", "track_churn"}


def metrics(**over):
    m = {
        "active_s": 0.0,
        "idle_s": float(WATCH_WINDOW_S),
        "transitions": 0,
        "max_active": 0,
        "max_parked": 0,
        "published": 0,
        "dropped": 0,
        "dropped_by_class": {},
        "luma": [],
        "ir": [],
    }
    m.update(over)
    return m


def test_watcher_asks_only_what_nobody_else_answers():
    for adaptive in (True, False):
        kinds = set(TelemetrySink._evaluate("patio", metrics(), adaptive))
        assert not kinds & RETIRED_KINDS
        assert kinds == (LIVE_KINDS if adaptive else {"drop_spike"})


def test_a_quiet_scene_trips_nothing():
    for kind, (tripped, _, _) in TelemetrySink._evaluate("patio", metrics(), True).items():
        assert not tripped, kind


def test_drop_spike_needs_volume_and_a_multiple():
    # Enough dropped, but published keeps pace — a busy scene, not a blind one.
    busy = metrics(dropped=DROP_SPIKE_MIN * 2, published=DROP_SPIKE_MIN * 2)
    assert not TelemetrySink._evaluate("west", busy, False)["drop_spike"][0]

    blind = metrics(dropped=DROP_SPIKE_MIN * 4, published=10)
    tripped, cleared, details = TelemetrySink._evaluate("west", blind, False)["drop_spike"]
    assert tripped and not cleared
    assert details["dropped"] == DROP_SPIKE_MIN * 4

    # Hysteresis: half the open threshold is what closes it again.
    assert TelemetrySink._evaluate("west", metrics(dropped=DROP_SPIKE_MIN // 2 - 1), False)[
        "drop_spike"
    ][1]


def test_active_pinned_reads_the_tracker_not_the_clock():
    pinned = metrics(active_s=float(WATCH_WINDOW_S), idle_s=0.0)
    assert TelemetrySink._evaluate("patio", pinned, True)["active_pinned"][0]

    # Same duty, but the tracker did see activity — that is the camera doing
    # its job, and the whole point of the kind is to tell the two apart.
    earned = metrics(active_s=float(WATCH_WINDOW_S), idle_s=0.0, max_active=1)
    assert not TelemetrySink._evaluate("patio", earned, True)["active_pinned"][0]


def test_aggregate_keeps_only_what_the_two_kinds_read():
    rows = [
        {
            "camera_slug": "shed",
            "source": "detector",
            "payload": json.dumps(
                {
                    "published": {"car": {"n": 7, "n_birth": 0}},
                    "dropped": {"car": {"conf": 5, "size": 2, "disabled": 900}},
                }
            ),
        },
        {
            "camera_slug": "shed",
            "source": "tracker",
            "payload": json.dumps({"births": 99, "flips": 99, "n_active": 2, "n_parked": 1}),
        },
    ]
    m = TelemetrySink._aggregate(rows)["shed"]
    assert m["published"] == 7
    # Disabled-class drops are by design and must never hold drop_spike open.
    assert m["dropped"] == 7
    assert m["max_active"] == 2 and m["max_parked"] == 1
    # Births and flips belong to the tracker's own mechanisms now.
    assert "births" not in m and "flips" not in m and "published_by_class" not in m


@pytest.mark.parametrize(
    "idle,target,ok",
    [(None, 4, True), (1, 4, True), (3, 4, True), (4, 4, False), (5, 4, False)],
)
def test_adaptive_needs_somewhere_to_decay_to(idle, target, ok):
    assert (adaptive_rate_error(idle, target) is None) is ok
