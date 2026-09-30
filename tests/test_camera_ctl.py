"""The one writer to a camera, and the promise it has to keep.

A temporary profile held only in memory is undone by a restart the wrong way:
the camera stays in it until somebody notices in the morning. So the way back
is written down before the override is applied, and read back by anything that
starts up afterwards.
"""

import asyncio
import json

from baba_api.camera_ctl import apply_profile, creds_for, restore, set_light


class FakePool:
    def __init__(self, held=None):
        self.held = held          # the row a camera is already held by, or None
        self.writes = []
        self.journal = []         # rows written to camera_setting_changes

    async def fetchval(self, sql, *_args):
        if "light_condition" in sql:
            return "ir"
        return (self.held or {}).get("reason")

    async def fetchrow(self, _sql, *_args):
        return self.held

    async def execute(self, sql, *args):
        if "camera_setting_changes" in sql:
            self.journal.append(args)
            return
        self.writes.append(("insert" if "INSERT" in sql else "delete", args))

    # `_journal` takes a connection out of the pool; this pool IS the connection.
    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                return pool

            async def __aexit__(self, *_exc):
                return False

        return _Ctx()


CREDS = creds_for("rtsp://admin:secret@192.168.10.13:554/Preview_01_main")


def test_the_address_and_login_come_from_the_stream_we_already_have():
    assert CREDS.host == "192.168.10.13"
    assert CREDS.user == "admin" and CREDS.password == "secret"
    # No stream, no control — rather than a half-built client that fails later.
    assert creds_for("rtsp://192.168.10.13:554/x") is None


def test_the_way_back_is_written_down_only_after_the_camera_took_the_change(monkeypatch):
    """A row with no override behind it would restore settings the camera never
    left, and the next sweep would read it as a crash that never happened."""
    import baba_api.camera_ctl as mod

    order = []

    async def _write(_creds, changes):
        order.append(("device", changes))
        return {"dayNight": "Color", "exposure": "Auto"}

    monkeypatch.setattr(mod, "write_settings", _write)
    pool = FakePool()
    asyncio.run(apply_profile(pool, "cam", CREDS, {"dayNight": "Black&White"},
                              reason="plate", seconds=30))
    assert order and order[0][0] == "device"
    kind, args = pool.writes[0]
    assert kind == "insert"
    assert json.loads(args[1]) == {"dayNight": "Color", "exposure": "Auto"}, \
        "the baseline is what the DEVICE had, not what we believed it had"


def test_a_camera_already_held_is_left_alone(monkeypatch):
    """Two callers fighting over one device is how a baseline becomes whatever
    the loser happened to read."""
    import baba_api.camera_ctl as mod

    async def _write(_creds, _changes):
        raise AssertionError("must not touch a held camera")

    monkeypatch.setattr(mod, "write_settings", _write)
    pool = FakePool(held={"reason": "plate", "baseline": {}})
    assert asyncio.run(apply_profile(pool, "cam", CREDS, {"x": 1},
                                     reason="light", seconds=30)) is False


def test_the_row_goes_only_after_the_camera_is_back(monkeypatch):
    """Deleted first, on a call that then fails, is a camera nobody knows is
    still overridden."""
    import baba_api.camera_ctl as mod

    order = []

    async def _call(_creds, cmd, _body):
        order.append(cmd)
        return {}

    monkeypatch.setattr(mod, "_call", _call)
    pool = FakePool(held={"baseline": json.dumps({"dayNight": "Color"}),
                          "reason": "plate"})
    assert asyncio.run(restore(pool, "cam", CREDS)) is True
    assert order == ["SetIsp"]
    assert pool.writes and pool.writes[0][0] == "delete"


def test_an_override_is_capped_however_long_the_caller_asked_for(monkeypatch):
    """The headlight override wants thirty seconds; a bug that asks for a day
    does not get one."""
    import baba_api.camera_ctl as mod

    async def _write(_creds, _changes):
        return {}

    monkeypatch.setattr(mod, "write_settings", _write)
    pool = FakePool()
    asyncio.run(apply_profile(pool, "cam", CREDS, {}, reason="bug", seconds=86400))
    assert pool.writes[0][1][3] == mod._MAX_OVERRIDE.total_seconds()


def test_on_and_off_are_the_field_that_persists(monkeypatch):
    """Measured 29.08 on West, outside its own schedule: writing `state` is a
    momentary flash. The camera put it back to 0 by itself three seconds later,
    because there `state` REPORTS whether the lamp is burning right now under
    the camera's AI trigger. `mode` is what persists, so `mode` is what on/off
    has to mean — and which vendor field that is must not leak to a caller."""
    import baba_api.camera_ctl as mod

    sent = {}

    async def _call(_creds, cmd, body):
        if cmd == "GetWhiteLed":
            return {"WhiteLed": {"mode": 3, "state": 0, "bright": 10,
                                 "LightingSchedule": {"StartHour": 21}}}
        sent["body"] = body[0]["param"]["WhiteLed"]
        return {}

    monkeypatch.setattr(mod, "_call", _call)
    asyncio.run(set_light(CREDS, on=False))
    assert sent["body"]["mode"] == 0
    assert "state" in sent["body"], "the rest of the object goes back untouched"
    assert sent["body"]["LightingSchedule"] == {"StartHour": 21}, \
        "switching off changes nothing but the mode"
    asyncio.run(set_light(CREDS, on=True, bright=250))
    assert sent["body"]["mode"] == 3
    assert sent["body"]["bright"] == 100, "brightness is clamped, not passed through"
    assert sent["body"]["LightingSchedule"] == {
        "StartHour": 0, "StartMin": 0, "EndHour": 23, "EndMin": 59}, \
        "arming opens the device's own timer all the way — the mode is the switch"


def test_the_login_is_found_in_the_query_string_too():
    """The same vendor writes both shapes: RTSP carries the login in the
    userinfo, the HTTP-FLV stream carries it in the query. Reading only the
    first excluded the camera over the driveway, which answered "no floodlight"
    while having one."""
    flv = creds_for(
        "http://192.168.10.12/flv?port=1935&app=bcs&stream=channel0_main.bcs"
        "&user=admin&password=secret"
    )
    assert flv.host == "192.168.10.12"
    assert flv.user == "admin" and flv.password == "secret"
    # Still no login anywhere, still no control.
    assert creds_for("http://192.168.10.12/flv?port=1935") is None


def test_a_lamp_goes_back_into_the_mode_IT_was_armed_in(monkeypatch):
    """"On" is not one value across this yard: west is armed in the vendor's AI
    mode and the camera over the gate in its night mode (3 and 1, read off both
    devices 29.08). Writing one of them everywhere converts the other camera's
    behaviour the first time the house turns its light back on — silently, since
    the lamp lights either way."""
    import baba_api.camera_ctl as mod

    sent = {}

    async def _call(_creds, cmd, body):
        if cmd == "GetWhiteLed":
            return {"WhiteLed": {"mode": 0, "state": 0, "bright": 85}}
        sent["body"] = body[0]["param"]["WhiteLed"]
        return {}

    monkeypatch.setattr(mod, "_call", _call)
    _led, was = asyncio.run(set_light(CREDS, on=True, armed_mode=1))
    assert sent["body"]["mode"] == 1
    assert was is None, "off carries no record of how the lamp was armed"


def test_the_mode_the_lamp_was_in_is_reported_so_it_can_be_remembered(monkeypatch):
    import baba_api.camera_ctl as mod

    async def _call(_creds, cmd, _body):
        if cmd == "GetWhiteLed":
            return {"WhiteLed": {"mode": 1, "state": 0, "bright": 85}}
        return {}

    monkeypatch.setattr(mod, "_call", _call)
    _led, was = asyncio.run(set_light(CREDS, on=False))
    assert was == 1, "the write that switches a lamp off is what destroys this"


def test_a_camera_that_does_not_speak_this_api_is_its_own_failure():
    """The Hikvision on the patio answers this endpoint with an HTTP 404 and a
    page saying it cannot locate the document (measured 29.08). Calling that
    "the device did not answer" asks a consumer to retry, with backoff, a camera
    that will never speak — four thousand log lines a day on DIDA's side."""
    import baba_api.camera_ctl as mod

    assert mod.has_no_light(mod.NotThisApi("GetWhiteLed: no such")) is True
    assert mod.has_no_light(RuntimeError("GetWhiteLed: no light in answer")) is True
    assert mod.has_no_light(TimeoutError("timed out")) is False


def test_the_stream_port_comes_from_the_stream_url():
    assert CREDS.stream_port == 554
    assert creds_for("rtsp://admin:secret@192.168.50.12/stream1").stream_port == 554
    flv = creds_for("http://192.168.10.12/flv?port=1935&user=admin&password=secret")
    assert flv.stream_port == 80, "the FLV stream is served by the same web server"


def _refused_connect(monkeypatch, mod, answers):
    """Every API call is refused, and the probe finds `answers` per port."""

    async def post(*_a, **_k):
        raise mod.httpx.ConnectError("All connection attempts failed")

    async def accepts(_host, port):
        return answers[port]

    monkeypatch.setattr(mod.httpx.AsyncClient, "post", post)
    monkeypatch.setattr(mod, "_accepts", accepts)


def test_a_camera_that_streams_but_refuses_this_api_has_no_light_we_can_reach(monkeypatch):
    """Cabin's TP-Links refuse port 80 while their RTSP port accepts (15.09).
    That is a camera standing there that will never speak this API — not one
    that is away — and it has to reach DIDA as a 404, not a 502 forever."""
    import baba_api.camera_ctl as mod
    import pytest

    _refused_connect(monkeypatch, mod, {80: False, 554: True})
    with pytest.raises(mod.NotThisApi) as exc:
        asyncio.run(mod.get_light(CREDS))
    assert mod.has_no_light(exc.value) is True


def test_a_rebooting_camera_is_still_only_away(monkeypatch):
    """Refusing everything is what a camera does for a moment while it boots.
    Calling that "no floodlight" would drop west's lamp for half an hour."""
    import baba_api.camera_ctl as mod
    import pytest

    _refused_connect(monkeypatch, mod, {80: False, 554: False})
    with pytest.raises(mod.httpx.ConnectError) as exc:
        asyncio.run(mod.get_light(CREDS))
    assert mod.has_no_light(exc.value) is False

    _refused_connect(monkeypatch, mod, {80: None, 554: True})
    with pytest.raises(mod.httpx.ConnectError):
        asyncio.run(mod.get_light(CREDS))


def test_a_camera_streaming_on_the_api_port_is_never_judged_by_it(monkeypatch):
    """The driveway's FLV stream lives on port 80 with the API: a refusal there
    says the whole device is away, whatever the probe would add."""
    import baba_api.camera_ctl as mod
    import pytest

    flv = creds_for("http://192.168.10.12/flv?port=1935&user=admin&password=secret")
    _refused_connect(monkeypatch, mod, {80: False})
    with pytest.raises(mod.httpx.ConnectError):
        asyncio.run(mod.get_light(flv))


def test_a_temporary_hold_is_written_into_the_journal_at_both_ends(monkeypatch):
    """A plate went unread on 02.09 — west's zone was 48 to 77 per cent
    saturated for twenty-one seconds as Ana's car crossed it — and whether the
    swap to the plate profile ever fired could only be guessed at afterwards.
    The journal held `profile_switch` and `manual` and nothing else, and the
    override table cannot answer it either: it is deleted on the way back out.

    So both ends are recorded, and the pair says how long the camera was held.
    """
    import baba_api.camera_ctl as mod

    async def _write(_creds, _changes):
        return {"exposure": "auto"}          # the baseline the device had

    async def _call(_creds, _cmd, _body):
        return {}

    monkeypatch.setattr(mod, "write_settings", _write)
    monkeypatch.setattr(mod, "_call", _call)

    pool = FakePool()
    asyncio.run(apply_profile(pool, "cam-1", CREDS, {"exposure": "1/2000"},
                              reason="headlight", seconds=45))
    assert len(pool.journal) == 1, "the hold is written down"
    _cam, source, _incident, band, before, after = pool.journal[0]
    assert source == "hold", "under a source the journal allows"
    assert band == "ir", "with the band the yard was in"
    # The payload is the reason the row exists. `source` says a hold happened;
    # only `after` says WHICH profile, now that the reason is a log line and
    # this table has no column for it.
    assert json.loads(before) == {"exposure": "auto"}, \
        "the baseline is what the DEVICE had, not what we believed it had"
    assert json.loads(after) == {"exposure": "1/2000"}, "and the profile written over it"

    held = FakePool(held={"baseline": {"exposure": "auto"}, "reason": "headlight"})
    asyncio.run(restore(held, "cam-1", CREDS))
    assert len(held.journal) == 1, "and so is the way back"
    _cam, source, _incident, _band, before, after = held.journal[0]
    assert source == "release"
    assert json.loads(after) == {"exposure": "auto"}, "restored to the baseline"
    # Deliberately empty, and this says so: the override row persists only the
    # baseline, so by the time a camera comes back nothing on record says what
    # it was wearing. The hold row is where that lives.
    assert json.loads(before) == {}


def test_a_journal_that_will_not_write_still_leaves_the_camera_held(monkeypatch):
    """The device has taken the profile and the override row is written by the
    time the journal is touched, so a throw out of it would report a hold that
    physically happened as not applied. The headlight watcher reads that answer:
    it would never set its hold or its cooldown, would fold its own overridden
    frames back into the baseline — the very thing its guard exists to stop —
    and would leave the camera in the plate profile until the sweeper expired
    it. A missing row is a lost record; a raised row was a broken camera.
    """
    import baba_api.camera_ctl as mod

    async def _write(_creds, _changes):
        return {"exposure": "auto"}

    async def _call(_creds, _cmd, _body):
        return {}

    monkeypatch.setattr(mod, "write_settings", _write)
    monkeypatch.setattr(mod, "_call", _call)

    class Hostile(FakePool):
        async def execute(self, sql, *args):
            if "camera_setting_changes" in sql:
                raise RuntimeError("check constraint")
            return await FakePool.execute(self, sql, *args)

    pool = Hostile()
    assert asyncio.run(apply_profile(pool, "cam-1", CREDS, {"exposure": "1/2000"},
                                     reason="headlight", seconds=45)) is True
    assert (pool.writes[0][0], ) == ("insert", ), "the override was still written"

    held = Hostile(held={"baseline": {"exposure": "auto"}, "reason": "headlight"})
    assert asyncio.run(restore(held, "cam-1", CREDS)) is True


def test_the_tracking_card_merges_the_row_it_shares(monkeypatch):
    """`app_settings.tracking_defaults` is shared: this card owns four motion
    values, `PUT /tunables` owns the anchor, identity and phantom knobs in the
    same key. `TrackingDefaults` has four fields and Pydantic drops the rest on
    the way in, so writing model_dump() wholesale deleted the other card's
    knobs — one drag of the Parked slider and the tracker lost its identity
    margin and both phantom thresholds, with a NOTIFY sending it to re-read the
    row it had just been emptied of.
    """
    import json as _json

    from baba_api.models import TrackingDefaults

    before = {"stillness_ratio": 0.08, "park_seconds": 60, "lost_seconds": 10,
              "reid_lost_seconds": 20, "identity_margin": 0.12,
              "phantom_min_births": 20, "phantom_min_span_s": 86400}
    payload = TrackingDefaults(stillness_ratio=0.2, park_seconds=90,
                               lost_seconds=10, reid_lost_seconds=20)

    after = {**before, **payload.model_dump()}
    assert after["park_seconds"] == 90, "the card's own value is written"
    for kept in ("identity_margin", "phantom_min_births", "phantom_min_span_s"):
        assert kept in after, f"{kept} survives a save of the other card"
    assert _json.loads(_json.dumps(after)) == after


def test_the_index_pass_unpacks_the_shape_it_is_given():
    """`_scan_segment_dir` returns four-tuples and `starts` keeps two of them.
    A probe added to the index pass unpacked four from `starts`, so every pass
    on every camera raised `not enough values to unpack` — recording carried on
    writing files while nothing was indexed for four hours, which means no
    Activity rows, no clips and nothing for retention to see.

    The loop that broke had no test because nothing in the gate reached
    `_index_once`. This asserts the two shapes against each other instead.
    """
    import inspect

    from baba_recorder import worker

    src = inspect.getsource(worker._scan_segment_dir)
    assert "out.append((f, ts, st.st_mtime, st.st_size))" in src, \
        "the scan yields (path, started_at, mtime, size)"

    idx = inspect.getsource(worker.CameraRecorder._index_once)
    assert "starts: list[tuple[Path, datetime]] = [(p, ts) for p, ts, _mt, _sz in scan]" in idx, \
        "and `starts` keeps the first two"
    assert "for path, ts in starts" in idx, \
        "so anything iterating `starts` unpacks two, not four"
    assert "_mt, _sz in starts" not in idx, "the four-tuple shape is the scan's, not starts'"
