"""The events→NATS bridge, and above all the doorbell subject it also mints.

A ring is written once, by the doorbell service, as an `events` row whose
payload names the action. The bridge used to discard that name and publish its
own, so BABA's own table said one thing and its bus said another; the DIDA
automation waiting on the table's word never fired, and the bell was silent for
a month while every part in the chain reported healthy.
"""

import asyncio
import json
from uuid import UUID

from baba_api.events_bridge import EventsNatsBridge

EVENT = UUID("33333333-3333-3333-3333-333333333333")
CAM = UUID("22222222-2222-2222-2222-222222222222")


class FakePool:
    def __init__(self, row):
        self._row = row

    async def fetchrow(self, _sql, *_args):
        return self._row


class FakeNats:
    def __init__(self):
        self.published = []

    async def publish(self, subject, data):
        self.published.append((subject, json.loads(data)))


def _row(kind, payload, at="2026-08-26T15:11:27+02:00"):
    from datetime import datetime

    return {
        "id": EVENT,
        "camera_id": CAM,
        "track_id": None,
        "kind": kind,
        "at": datetime.fromisoformat(at),
        "payload": payload,
        "camera_slug": "gate",
        "camera_name": "Gate",
    }


def _run(row):
    nc = FakeNats()
    bridge = EventsNatsBridge("dsn", FakePool(row), nc)
    asyncio.run(bridge._publish(json.dumps({"id": str(EVENT)})))
    return nc.published


def test_bell_carries_the_action_the_row_recorded():
    published = _run(_row("doorbell_press", {"action": "ring", "source": "reolink_baichuan"}))
    bell = [m for s, m in published if s == f"baba.bell.{CAM}"]
    assert bell == [{"camera_id": str(CAM), "action": "ring"}]


def test_bell_rides_its_own_subject_as_well_as_the_event_stream():
    subjects = [s for s, _ in _run(_row("doorbell_press", {"action": "ring"}))]
    assert subjects == [f"baba.events.{CAM}", f"baba.bell.{CAM}"]


def test_a_non_bell_event_mints_no_bell_subject():
    subjects = [s for s, _ in _run(_row("zone_enter", {"zone": "drive"}))]
    assert subjects == [f"baba.events.{CAM}"]


def test_payload_is_decoded_when_the_driver_hands_back_text():
    published = _run(_row("doorbell_press", json.dumps({"action": "ring"})))
    bell = [m for s, m in published if s == f"baba.bell.{CAM}"]
    assert bell == [{"camera_id": str(CAM), "action": "ring"}]


def test_an_arrival_rides_its_own_subject():
    """A consumer that wants arrivals would otherwise take the whole event
    stream to find them: 29510 `object_parked` rows a week for two useful ones,
    over a link that is 0.79 Mbit/s at one of the sites. Same reasoning that
    already gives a doorbell press its own subject."""
    published = _run(_row("vehicle_arrived", {
        "place": "P4", "name": "Goran", "stood_s": None,
        "started_at": "2026-08-27T15:54:53+00:00",
        "ended_at": "2026-08-27T15:55:53+00:00"}))
    place = [m for s, m in published if s == f"baba.place.{CAM}"]
    assert len(place) == 1, "one publish on the place subject"
    assert place[0]["kind"] == "vehicle_arrived" and place[0]["place"] == "P4"
    assert place[0]["name"] == "Goran", "the payload rides along, flattened"
    # And it still goes down the ordinary stream, like every other event.
    assert any(s == f"baba.events.{CAM}" for s, _ in published)


def test_an_ordinary_event_does_not_touch_the_place_subject():
    published = _run(_row("object_parked", {"class_name": "car"}))
    assert not [m for s, m in published if s.startswith("baba.place.")]
