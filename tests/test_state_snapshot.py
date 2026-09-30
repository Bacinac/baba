"""What travels on the per-camera state plane, and what deliberately does not.

DIDA arms the yard light off BABA's verdict about darkness rather than its own
weather station, because the station cannot see the range that matters: its
first non-zero reading is 0.08 W/m2 — a single rung, 10 lux — and west has a
colour picture again about six minutes before that. A pyranometer built for a
quarter of a million lux quantises above the point where dusk is decided.
"""

import asyncio
import json

from baba_event_manager.state_snapshot import StateSnapshotPublisher


class Nats:
    def __init__(self):
        self.sent = []

    async def publish(self, subject, payload):
        self.sent.append((subject, json.loads(payload)))


def test_the_illumination_band_travels_but_the_number_behind_it_does_not():
    """The band carries all of the hysteresis already — sticky colour bands
    plus the debounce on consecutive buckets — which is exactly what a mirror
    must not re-derive. `luma_avg` stays behind: it moves every minute on a
    still scene, and a snapshot that publishes on change would turn every quiet
    camera into a once-a-minute talker saying nothing."""
    nc = Nats()
    pub = StateSnapshotPublisher(nc)
    asyncio.run(pub.update_light("cam-uuid", "West", "dark"))
    asyncio.run(pub.update_light("cam-uuid", "West", "dark"))
    assert len(nc.sent) == 1, "a band that did not change stays off the bus"
    subject, body = nc.sent[0]
    assert subject == "baba.state.cam-uuid"
    assert body["light_condition"] == "dark"
    assert "luma_avg" not in body

    asyncio.run(pub.update_light("cam-uuid", "West", "normal"))
    assert nc.sent[-1][1]["light_condition"] == "normal"
    # A camera whose band we no longer know must not keep asserting the last
    # one: the field goes, and the consumer stops mirroring it.
    asyncio.run(pub.update_light("cam-uuid", "West", None))
    assert "light_condition" not in nc.sent[-1][1]


def test_a_band_measured_on_a_picture_we_changed_is_not_the_yards_band():
    """The headlight swap holds west monochrome for 45 seconds. Straddling two
    telemetry buckets that reads as two consecutive `ir` samples — enough to
    clear the debounce, switch the setpoint profile, and now enough to arm a
    lamp — on nothing that happened outside the camera."""
    from baba_event_manager.telemetry import LIGHT_DEBOUNCE_BUCKETS, TelemetrySink

    class Pool:
        def __init__(self, held):
            self.held = held
            self.switched = []

        async def fetchval(self, sql, *args):
            if "camera_profile_override" in sql:
                return 1 if self.held else None
            return "normal"

    async def _run(held):
        pool = Pool(held)
        sink = TelemetrySink(pool)
        switched = []

        async def _switch(slug, new):
            switched.append((slug, new))

        sink._switch_light = _switch
        for _ in range(LIGHT_DEBOUNCE_BUCKETS):
            await sink._track_light("west", {"luma_avg": 20.0, "ir_ratio": 1.0})
        return switched

    assert asyncio.run(_run(held=False)), "an honest dark reading still switches"
    assert asyncio.run(_run(held=True)) == [], "one taken while we hold the camera does not"
