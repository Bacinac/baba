"""The presence registry's sustain gate and debounce, against a fake pool.

The gate exists because of a day-one phantom: a short-lived track of Marko's
face at a bad angle matched 'Ana' once, and a durable episode stood open for
half an hour while she was demonstrably away. One verdict is noise; twenty
seconds of them is a person.
"""

import asyncio
from datetime import UTC
from uuid import UUID

import baba_event_manager.presence as presence_mod
from baba_event_manager.presence import PresenceRegistry

GID = UUID("11111111-1111-1111-1111-111111111111")
CAM = UUID("22222222-2222-2222-2222-222222222222")


class FakePool:
    def __init__(self):
        self.executed = []

    async def execute(self, sql, *args):
        self.executed.append((sql, args))

    async def fetchval(self, sql, *args):
        # The registry's upsert RETURNING present_since — echo the opened_at
        # argument back the way the row would.
        self.executed.append((sql, args))
        return args[3] if len(args) > 3 else None

    async def fetch(self, sql, *args):
        return []


class Clock:
    def __init__(self):
        self.mono = 1000.0
        self.wall = 1_753_800_000.0

    def advance(self, s):
        self.mono += s
        self.wall += s


def _wire(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(presence_mod.time, "monotonic", lambda: clock.mono)
    monkeypatch.setattr(presence_mod.time, "time", lambda: clock.wall)
    pool = FakePool()
    return clock, pool, PresenceRegistry(pool)


def test_single_verdict_is_noise(monkeypatch):
    _clock, pool, reg = _wire(monkeypatch)
    asyncio.run(reg.observe(CAM, {GID: ("Ana", "face")}))
    assert pool.executed == []


def test_sustained_verdict_opens_backdated(monkeypatch):
    clock, pool, reg = _wire(monkeypatch)
    first_wall = clock.wall

    async def run():
        await reg.observe(CAM, {GID: ("Marko", "face")})
        clock.advance(21)
        await reg.observe(CAM, {GID: ("Marko", "face")})

    asyncio.run(run())
    assert len(pool.executed) == 1
    _sql, args = pool.executed[0]
    opened_at = args[3]
    # present_since is the FIRST sighting: committing late must not shorten
    # the recorded stay.
    assert opened_at is not None and opened_at.timestamp() == first_wall


def test_expired_pending_starts_over(monkeypatch):
    clock, pool, reg = _wire(monkeypatch)

    async def run():
        await reg.observe(CAM, {GID: ("Marko", "face")})
        clock.advance(90)  # beyond the pending TTL — whatever it was is gone
        await reg.observe(CAM, {GID: ("Marko", "face")})
        clock.advance(5)   # sustained only 5 s since the fresh first sighting
        await reg.observe(CAM, {GID: ("Marko", "face")})

    asyncio.run(run())
    assert pool.executed == []


def test_confirmations_are_debounced_but_face_upgrade_is_not(monkeypatch):
    clock, pool, reg = _wire(monkeypatch)

    async def run():
        await reg.observe(CAM, {GID: ("Marko", "body")})
        clock.advance(21)
        await reg.observe(CAM, {GID: ("Marko", "body")})  # opens (body)
        clock.advance(5)
        await reg.observe(CAM, {GID: ("Marko", "body")})  # inside debounce: no-op
        await reg.observe(CAM, {GID: ("Marko", "face")})  # upgrade: writes NOW

    asyncio.run(run())
    assert len(pool.executed) == 2
    assert pool.executed[0][1][2] == "body"
    assert pool.executed[1][1][2] == "face"


def test_forget_re_arms_the_gate(monkeypatch):
    clock, pool, reg = _wire(monkeypatch)

    async def run():
        await reg.observe(CAM, {GID: ("Marko", "face")})
        clock.advance(21)
        await reg.observe(CAM, {GID: ("Marko", "face")})  # open episode
        assert reg.since_of(CAM, GID) is not None  # cache fed by RETURNING
        reg._forget([{"global_id": GID, "camera_id": CAM}])
        assert reg.since_of(CAM, GID) is None      # and cleared on close
        clock.advance(120)
        await reg.observe(CAM, {GID: ("Marko", "face")})  # first sighting again

    asyncio.run(run())
    # only the original open — the post-forget sighting is pending, not a row
    assert len(pool.executed) == 1


def test_a_car_parked_against_the_frame_edge_still_files_its_visit():
    """17.08: Marko's car stood in P1 for thirty-four minutes at a box whose y2
    cleared west's edge threshold by 4.4 px. The parked transition never fired,
    so no visit was written and the place registry had nothing to bind. It had
    worked the day before only because shed filed that arrival instead."""
    from baba_event_manager.__main__ import came_to_rest

    # Stopping at the edge is still not parking — that is a car on its way out.
    assert not came_to_rest("active", "stationary", at_frame_edge=True, already_emitted=False)
    # But sustained stillness is, and a departing car never spends it.
    assert came_to_rest("stationary", "parked", at_frame_edge=True, already_emitted=False)
    # Once said, not said again for the same spell.
    assert not came_to_rest("parked", "parked", at_frame_edge=True, already_emitted=True)
    # Away from the edge nothing changed: the first stillness reports.
    assert came_to_rest("active", "stationary", at_frame_edge=False, already_emitted=False)


def test_a_car_that_stopped_is_not_a_drive_through():
    """26.08: the same P1, a different gate. West's Carport and Driveway rules
    carry min_dwell_ms=5000 and fire only while the subject is moving, so a car
    that turns in and stops within a few seconds never clears them — and the
    finalize dropped the track, leaving `claim_place` to report P1 occupied
    with no vehicle behind it. Ana's car on P2 and Nika's on P4 park far
    enough away to spend the five seconds, which is why only P1 kept failing."""
    from baba_event_manager.__main__ import zone_gate_verdict

    # The arrival that was being deleted: zones touched, no enter, came to rest.
    assert zone_gate_verdict(True, False, True, False) is None
    # What the gate is actually for still goes: crossed a zone and drove on.
    assert zone_gate_verdict(True, False, False, False) == "sub-dwell"
    # Coming to rest is no licence to ignore the zones themselves — a car that
    # parks on the road in the corner of the frame is still the operator's noise.
    assert zone_gate_verdict(False, False, True, False) == "off-zone"
    assert zone_gate_verdict(False, True, True, False) == "off-zone"
    # And a track that cleared the dwell was never in question.
    assert zone_gate_verdict(True, True, False, False) is None


def test_no_zone_gate_hides_a_person():
    """11.09: a parcel carried through the gate, three and a half seconds
    inside the polygon against a five-second dwell — and measured over thirty
    days, 94 of the 143 people that camera lost never touched a polygon at
    all. Both gates aim at vehicles and at traffic clipping a corner of the
    frame; between them they recorded one visitor in six. `ignore` zones, not
    these, are how a part of the frame stops being ours."""
    from baba_event_manager.__main__ import zone_gate_verdict

    # Crossed a zone too briefly, and never touched one: both pass.
    assert zone_gate_verdict(True, False, False, True) is None
    assert zone_gate_verdict(False, False, False, True) is None
    # A vehicle in the same two positions is still the operator's noise.
    assert zone_gate_verdict(True, False, False, False) == "sub-dwell"
    assert zone_gate_verdict(False, False, False, False) == "off-zone"


class FakeConn:
    """Enough of a connection to watch what claim_place decides."""

    def __init__(self, arrived=True):
        self.arrived = arrived
        self.rows = []
        self.seen_sql = ""

    async def fetchval(self, sql, *args):
        self.seen_sql = sql
        return self.arrived

    async def fetchrow(self, sql, *args):
        self.rows.append(args)
        return {"id": UUID("33333333-3333-3333-3333-333333333333")}

    async def fetch(self, sql, *args):
        return []


def test_a_place_reported_occupied_by_nothing_opens_no_episode():
    """Twenty nights running, shed's P1 went `present` at 21:0x and `empty` at
    05:1x with no vehicle within three minutes of either — every prototype it
    classifies against was captured in July daylight, and under IR it was
    guessing. Each guess used to become a nameless occupant standing in the
    registry until morning."""
    from baba_core.occupancy import claim_place

    conn = FakeConn(arrived=False)
    asyncio.run(claim_place(conn, "P1"))
    assert conn.rows == []
    # Not merely "some vehicle track exists": it must have driven, and been
    # seen by a camera that watches this place.
    assert "t.ever_active" in conn.seen_sql
    assert "sr.place = $1" in conn.seen_sql


def test_an_attested_arrival_opens_an_unnamed_episode():
    """The place opens on evidence that a car arrived and on nothing else.
    WHICH car is decided later by the read that preceded the fill — guessing
    it here, from whichever track ended last, is what put one car in two
    places."""
    from baba_core.occupancy import claim_place

    conn = FakeConn(arrived=True)
    asyncio.run(claim_place(conn, "P1"))
    assert conn.rows == [("P1",)]



# The reconciler joins two event streams. These tests pin its ORCHESTRATION —
# demote before name, one read consumed once, a guest named without a gid.
# The SQL itself is proven against a schema clone of production (the dryrun
# replay), not re-implemented in a fake.

READ = UUID("66666666-6666-6666-6666-666666666666")
EPISODE = UUID("77777777-7777-7777-7777-777777777777")


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class ReconcileConn:
    """Scripted connection: hands back the joined rows the SELECT would,
    records every write in order."""

    def __init__(self, joined, stale_places=(), name_lands=True):
        self.joined = list(joined)
        self.stale_places = list(stale_places)
        self.name_lands = name_lands
        self.writes = []

    def transaction(self):
        return _Tx()

    async def fetch(self, sql, *args):
        if "JOIN LATERAL" in sql:
            return self.joined
        self.writes.append(("demote", args))
        return [{"place": p} for p in self.stale_places]

    async def fetchrow(self, sql, *args):
        self.writes.append(("name", args))
        return {"id": args[0]} if self.name_lands else None


def _joined(gid=GID, read_id=READ, plate="ZG9420GZ"):
    return {"id": EPISODE, "place": "P1", "read_id": read_id,
            "global_id": gid, "plate_text": plate}


def test_a_fill_takes_the_read_that_walked_in_ahead_of_it():
    """The whole binding: the episode is named with the read's identity and
    the read is recorded on the row — nothing about tracks, geometry, or
    which camera ended last."""
    from baba_core.occupancy import reconcile_places

    conn = ReconcileConn([_joined()])
    asyncio.run(reconcile_places(conn))
    assert [w[0] for w in conn.writes] == ["demote", "name"]
    assert conn.writes[-1][1] == (EPISODE, GID, READ)


def test_a_matched_identity_evicts_its_own_stale_episode_first():
    """A car proven to have just arrived HERE cannot still be standing where a
    stuck view left it. The demote runs BEFORE the naming, in the same
    transaction, so the one-car-one-place index never aborts the join."""
    from baba_core.occupancy import reconcile_places

    conn = ReconcileConn([_joined()], stale_places=["P2"])
    asyncio.run(reconcile_places(conn))
    demote = conn.writes[0]
    assert demote[0] == "demote"
    # The demote excludes the episode being named, and names the identity.
    assert demote[1] == (GID, EPISODE)
    assert conn.writes[1][0] == "name"


def test_a_guest_plate_names_the_place_without_an_identity():
    """26.08 15:13: ZG-9906-KB, enrolled nowhere, parked in P2 for twelve
    minutes and the badge showed nothing. The read is the name — the raw
    registration renders until somebody enrols it."""
    from baba_core.occupancy import reconcile_places

    conn = ReconcileConn([_joined(gid=None, plate="ZG9906KB")])
    asyncio.run(reconcile_places(conn))
    # No identity, no demote — nothing can be standing elsewhere.
    assert [w[0] for w in conn.writes] == ["name"]
    assert conn.writes[0][1] == (EPISODE, None, READ)


def test_a_name_that_does_not_land_rolls_its_demote_back():
    """Two fills can share one candidate read in a single pass; the loser's
    naming lands zero rows. The demote that ran ahead of it was justified by
    nothing, so the step raises inside its transaction (Postgres rolls the
    demote back — proven on the production-clone replay) and the pass simply
    moves on rather than crashing or stripping a correct episode."""
    from baba_core.occupancy import reconcile_places

    conn = ReconcileConn([_joined()], stale_places=["P2"], name_lands=False)
    asyncio.run(reconcile_places(conn))
    # Both statements were attempted, in order, and the loop survived.
    assert [w[0] for w in conn.writes] == ["demote", "name"]


def test_no_read_in_the_window_leaves_the_place_honestly_unnamed():
    """A car whose plate the OCR could not resolve stays `unknown` — measured:
    every stuck-unknown episode in 21 days had only EMPTY reads nearby, and a
    guessed name is the failure this registry exists to end."""
    from baba_core.occupancy import reconcile_places

    conn = ReconcileConn([])
    asyncio.run(reconcile_places(conn))
    assert conn.writes == []


def _ep(place, released, name="Marko's car", cams=("cam-a",), occupied=0.0):
    from datetime import datetime, timedelta
    base = datetime(2026, 8, 23, 9, 0, tzinfo=UTC)
    return {"place": place, "occupied_since": base + timedelta(seconds=occupied),
            "released_at": base + timedelta(seconds=released), "global_id": GID,
            "evidence": "plate", "name": name, "kind": "vehicle", "cameras": list(cams),
            "views": [{"id": c, "slug": c, "name": c.upper()} for c in cams]}


def test_who_is_entitled_to_a_vote_has_one_definition():
    """Two callers asked the same question and answered it differently: the
    release vote filtered on `last_label_raw` while the episode repair read
    `current_state`. On 31.08 west committed `empty` for P2 while shed still
    stood at `present`; shed had been dropped from the vote by a single
    uncertain read, the place released against a view that positively saw the
    car, and the repair reopened an episode on that same standing `present` —
    two rows sharing one arrival second, and departures announced at 19:44 and
    19:45.

    A view abstains when its camera has stopped producing frames and when it
    has committed `blinded` — 27.08, P4 released the moment west's IR lit the
    spiderweb. It does not abstain for being momentarily unsure.
    """
    from baba_core.occupancy import _VIEW_SAYS, release_place_if_free

    conn = ReleaseConn(any_present=False, any_empty=True)
    asyncio.run(release_place_if_free(conn, "P2"))
    assert _VIEW_SAYS in conn.seen_sql

    # A camera producing no frames has no vote; a blinded one is excluded by
    # the states the vote is taken over.
    assert "s.last_label_raw IS DISTINCT FROM 'stale'" in _VIEW_SAYS
    assert "s.current_state IN ('present', 'empty')" in _VIEW_SAYS
    # Being unsure once is not blindness: the view goes on saying what it last
    # saw for certain, and that is a vote.
    assert "'unknown'" not in _VIEW_SAYS


class AnnounceConn:
    """Captures the announcement SQL — what is being asked for is the point."""

    def __init__(self, rows=()):
        self.sql = []
        self._rows = list(rows)

    async def fetch(self, sql, *_args):
        self.sql.append(" ".join(sql.split()))
        return self._rows


def test_an_arrival_and_a_departure_are_each_announced_once():
    """The bus carried `parked` — who stands there NOW — and a state cannot
    describe something that already happened, so nothing ever said a car HAD
    arrived or HAD left. An episode is the record of both ends; both are said
    out loud from it, and the events table is what remembers they were said."""
    from baba_core.occupancy import announce_episodes

    conn = AnnounceConn([{"place": "P4", "name": "Goran"}])
    asyncio.run(announce_episodes(conn))
    kinds = [k for k in ("vehicle_arrived", "vehicle_left")
             if any(f"'{k}'" in q for q in conn.sql)]
    assert kinds == ["vehicle_arrived", "vehicle_left"]
    for q in conn.sql:
        assert "NOT EXISTS" in q and "payload->>'episode_id'" in q, \
            "said once, and the events table is the only record of that"


def test_a_departure_reaches_back_to_the_walk_and_an_arrival_does_not():
    """A departure does not begin with the car. Ana came out of the house at
    18:15:35 on 03.09 and the place emptied at 18:16:43 — the sixty-second
    bookend opened the clip at 18:15:43, eight seconds after she was already on
    camera, so the scene started from the car reversing.

    Measured across every departure on record carrying a person track on the
    witness camera, the walk begins 34, 48, 68 and 98 seconds ahead of the
    place emptying. The arrival is the other way round: the car comes first and
    whoever was in it gets out afterwards, so it keeps the plain bookend.

    What the window actually comes out as is proven by replaying this against a
    clone of production, not by a fake that would only restate the SQL here.
    """
    from baba_core.occupancy import (
        _APPROACH_MAX_S,
        _BOOKEND_S,
        announce_episodes,
    )

    assert _APPROACH_MAX_S > _BOOKEND_S, "the cap has to be able to reach past"

    conn = AnnounceConn()
    asyncio.run(announce_episodes(conn))
    arrival = next(q for q in conn.sql if "'vehicle_arrived'" in q)
    departure = next(q for q in conn.sql if "'vehicle_left'" in q)

    assert "class_id = 0" in departure and "LEAST" in departure, \
        "the departure looks for the person who walked to the car"
    assert "class_id" not in arrival, "an arrival is the car's, not the walk's"
    # Never shorter than it was: the bookend is the ceiling of the LEAST.
    assert f"interval '{_BOOKEND_S} seconds'" in departure
    # And a departure with no person track to reach for gets the whole cap
    # rather than falling back to the minute — two of the six real departures
    # on record left no track on the witness camera at all.
    assert departure.count(f"interval '{_APPROACH_MAX_S} seconds'") == 3, \
        "cap as the floor, as the COALESCE default, and as the track window"


def test_an_announcement_carries_a_bookend_not_the_whole_stay():
    """A car that stood fourteen hours is two moments and a number. Both
    windows end on the registry's own moment; how long it stood rides as
    `stood_s`, which is a number, not a span to sit through."""
    from baba_core.occupancy import _APPROACH_MAX_S, announce_episodes

    conn = AnnounceConn()
    asyncio.run(announce_episodes(conn))
    arrival = next(q for q in conn.sql if "'vehicle_arrived'" in q)
    assert "'started_at', o.occupied_since - interval '60 seconds'" in arrival
    assert "'ended_at', o.occupied_since" in arrival
    departure = next(q for q in conn.sql if "'vehicle_left'" in q)
    assert "'ended_at', o.released_at" in departure
    assert "'stood_s'" in departure
    # The departure reaches back for the walk instead of a flat minute, and no
    # further than the cap however long somebody stood about.
    assert f"interval '{_APPROACH_MAX_S} seconds'" in departure


def test_the_witness_is_the_camera_that_read_the_plate():
    """A place is watched by more than one camera and only one of them was
    pointed at the approach — which is why it could read the plate at all.
    Alphabetical order filed Ana's arrival against Shed on 28.08 while West
    held the drive-in, the open gate and the registration."""
    from baba_core.occupancy import announce_episodes

    conn = AnnounceConn()
    asyncio.run(announce_episodes(conn))
    for q in conn.sql:
        assert "ORDER BY (sr.camera_id = pr.camera_id) DESC" in q, \
            "the camera that read the plate wins; the alphabet only breaks ties"


def test_an_announcement_never_replays_the_registry():
    """The first pass after a deploy must not dump every episode ever onto the
    feed and the bus, dated across the month they happened."""
    from baba_core.occupancy import announce_episodes

    conn = AnnounceConn()
    asyncio.run(announce_episodes(conn))
    assert all("> now() - interval '1 hour'" in q for q in conn.sql)


class ReleaseConn:
    """Scripted views for one place, and a record of whether it was closed."""

    def __init__(self, any_present, any_empty):
        self.views = {"any_present": any_present, "any_empty": any_empty}
        self.closed = False
        self.seen_sql = ""

    async def fetchrow(self, sql, *args):
        if "bool_or" in sql:
            self.seen_sql = sql
            return self.views
        self.closed = True
        return {"global_id": None, "held": 42}


def test_a_place_no_view_can_see_is_held_not_released():
    """28.08: west's IR lamp lights a spiderweb across the left of its lens
    from about 21:00, P4 lives in that strip, and no second camera watches it.
    A car that stood there all night was released at 21:02 — the moment the
    web lit up — and its real departure eleven hours later closed a different,
    nameless episode. `unknown` means the camera lost sight of the place, and
    that is not a car leaving."""
    from baba_core.occupancy import release_place_if_free

    blind = ReleaseConn(any_present=False, any_empty=False)
    asyncio.run(release_place_if_free(blind, "P4"))
    assert not blind.closed


def test_a_place_somebody_reads_empty_is_released():
    """The other half of the same rule: a positive `empty` from any view still
    closes the episode, so holding a blind place costs nothing once one of
    them can see again."""
    from baba_core.occupancy import release_place_if_free

    seen_empty = ReleaseConn(any_present=False, any_empty=True)
    asyncio.run(release_place_if_free(seen_empty, "P4"))
    assert seen_empty.closed

    # And a view that still sees the car outranks one that reads empty —
    # the shed half of P1 flaps at dusk while west holds it correctly.
    disagreeing = ReleaseConn(any_present=True, any_empty=True)
    asyncio.run(release_place_if_free(disagreeing, "P1"))
    assert not disagreeing.closed


class DepartConn:
    """Hands back the departure join's row, records the naming."""

    def __init__(self, joined):
        self.joined = list(joined)
        self.seen_sql = ""
        self.writes = []

    async def fetch(self, sql, *args):
        self.seen_sql = sql
        return self.joined

    async def fetchrow(self, sql, *args):
        self.writes.append(args)
        return {"id": args[0]}


def test_the_read_taken_on_the_way_out_names_the_place_that_closed():
    """`SI421KL`, ocr 1.00, read at 08:45:02 as the car passed the approach;
    the place closed two seconds later as an unidentified vehicle and the read
    was decided nine minutes after that. The arrival join is right to refuse a
    read that follows a fill — but refusing it left the only evidence of who
    had been standing there unused."""
    from baba_core.occupancy import name_departures

    conn = DepartConn([{"id": EPISODE, "place": "P4", "read_id": READ,
                        "global_id": GID, "plate_text": "SI421KL"}])
    asyncio.run(name_departures(conn))
    assert conn.writes == [(EPISODE, GID, READ)]


def test_a_departure_never_outbids_a_fill_for_the_same_read():
    """An arrival still owns any read inside its own window; only what the
    join has already declined is left for a departure to take."""
    from baba_core.occupancy import name_departures

    conn = DepartConn([])
    asyncio.run(name_departures(conn))
    assert "op.released_at IS NULL" in conn.seen_sql
    assert "op.occupied_since - interval" in conn.seen_sql
    assert conn.writes == []


def test_a_departure_is_named_by_the_registry_not_by_the_leaving_track():
    """Measured over thirty days: every departure from P1 produced no identified
    vehicle track, while the episode it ended had carried the name on plate
    evidence since the arrival. The leaving car is the poorer witness."""
    from datetime import datetime, timedelta

    from baba_core.parked import episode_for

    base = datetime(2026, 8, 23, 9, 0, tzinfo=UTC)
    eps = [_ep("P1", 600, cams=("cam-a",)), _ep("P2", 640, cams=("cam-b",))]
    # A camera that watches P1 gets P1's episode, not the P2 one 40 s away.
    hit = episode_for(eps, base + timedelta(seconds=605), "cam-a")
    assert hit is not None and hit["place"] == "P1"
    # A camera with no view of either place claims neither.
    assert episode_for(eps, base + timedelta(seconds=605), "cam-z") is None
    # And nothing matches an hour away.
    assert episode_for(eps, base + timedelta(seconds=4000), "cam-a") is None


def test_a_departure_no_track_witnessed_still_becomes_a_row():
    """West produces no track at all for some departures, so the feed simply
    had no row for a car that demonstrably left a spot it held for hours."""
    from baba_core.parked import episode_departure_row

    row = episode_departure_row(_ep("P1", 3600, occupied=0.0, cams=(str(CAM),)))
    assert row is not None
    # Every feed row is played from a camera, and the id is parsed as a UUID
    # downstream — an empty one took the whole feed down with a 500.
    UUID(row["camera"]["id"])
    assert row["kind"] == "departure"
    assert row["identity"]["name"] == "Marko's car"
    assert row["visit_duration_s"] == 3600.0
    assert row["id"].startswith("dep:P1:")
    # No camera watches the place -> no row, rather than an unplayable one.
    orphan = _ep("P9", 3600)
    orphan["views"] = None
    assert episode_departure_row(orphan) is None
    # asyncpg hands jsonb back as text; a row that arrives undecoded must not
    # take the feed down — twice now it did.
    raw = _ep("P9", 3600)
    raw["views"] = '[{"id": "x"}]'
    assert episode_departure_row(raw) is None


def test_a_departure_row_wears_the_camera_the_feed_was_filtered_to():
    """Emitted unfiltered, a departure from a place West watches turned up in
    an Activity list filtered to Patio and played West's footage. The row is
    shown and played from the camera that was asked for, or not at all."""
    from baba_core.parked import episode_departure_row

    ep = _ep("P1", 3600, cams=("cam-west", "cam-shed"))
    assert episode_departure_row(ep, "cam-shed")["camera"]["id"] == "cam-shed"
    # Asked for a camera that does not watch this place: no row, not a wrong one.
    assert episode_departure_row(ep, "cam-patio") is None
    # Unfiltered, the first view carries it.
    assert episode_departure_row(ep)["camera"]["id"] == "cam-west"


def test_a_stay_is_announced_when_its_name_is_known_or_provably_not_coming():
    """Both ends waited wrongly. The arrival always sat out a fixed six minutes
    even when the plate had already named it, and the departure waited for
    nothing at all: on 29.08 P2 was announced unnamed at 11:21:36 while the read
    that named it was decided at 11:24:36 — the registry learned who it was and
    nobody downstream ever heard.

    A named episode has nothing left to wait for. A nameless one waits longer
    than the reader's own latency (3:00 and 6:06 measured from the edge of the
    episode to the decision) and only then says it does not know.
    """
    from baba_core.occupancy import _ANNOUNCE_GRACE, announce_episodes

    conn = AnnounceConn()
    asyncio.run(announce_episodes(conn))
    arrival, departure = conn.sql
    for sql, edge in ((arrival, "occupied_since"), (departure, "released_at")):
        assert f"o.{edge} IS NOT NULL AND (" in sql
        assert "o.global_id IS NOT NULL OR o.plate_read_id IS NOT NULL" in sql
        assert f"o.{edge} < now() - interval '{_ANNOUNCE_GRACE}'" in sql
    assert "'vehicle_arrived'" in arrival and "'vehicle_left'" in departure


def test_one_departure_is_one_tile_from_the_walk_to_the_car_leaving():
    """A person walking to their car and that car driving off is one thing to
    watch. It arrived as two tiles: the registry's row, which stops when the
    place reads empty, and the person's row, which stops when the car clears
    the frame eight seconds later. Whichever the operator clicked showed half
    of it, and the merge that was meant to prevent that asked whether the
    person's INSTANT fell inside the car's window — which, ending later, it
    never did.

    Overlap is the question, and the tile that comes out spans both.
    """
    from baba_core.parked import row_covering

    cam = {"id": "cam-1", "slug": "west", "name": "West"}
    registry = {"camera": cam, "class_name": "car", "kind": "departure",
                "started_at": "2026-09-03T16:15:35+00:00",
                "ended_at": "2026-09-03T16:16:43+00:00"}
    person = {"camera": cam, "class_name": "person", "kind": "left_with_vehicle",
              "identity": {"name": "Ana", "kind": "person"},
              "started_at": "2026-09-03T16:15:35+00:00",
              "ended_at": "2026-09-03T16:16:51+00:00"}

    host = row_covering([registry], person)
    assert host is registry, "the two ends of one departure find each other"

    start = min(host["started_at"], person["started_at"])
    end = max(host["ended_at"], person["ended_at"])
    assert start == "2026-09-03T16:15:35+00:00", "opens on the walk"
    assert end == "2026-09-03T16:16:51+00:00", "closes on the car leaving frame"

    # A different camera, or a departure an hour away, is not the same thing.
    assert row_covering([{**registry, "camera": {"id": "cam-2"}}], person) is None
    apart = {**person, "started_at": "2026-09-03T17:15:35+00:00",
             "ended_at": "2026-09-03T17:16:51+00:00"}
    assert row_covering([registry], apart) is None


def test_a_car_driving_in_and_the_walk_out_of_it_are_one_tile():
    """The mirror of a departure, split the same way: west had Ana's car from
    17:44:44 to 17:45:05 on 03.09 and her walk from 17:45:30 to 17:45:52, two
    tiles, neither showing the arrival whole.

    The pause between them is somebody sitting in a stopped car before getting
    out — twenty-five seconds here, thirty-two on the mirror departure — so it
    is not a break, and the same gap the feed already allows inside one
    presence covers it.
    """
    from baba_core.parked import fold_arrival_walk

    cam = {"id": "cam-1", "slug": "west", "name": "West"}
    car = {"camera": cam, "class_name": "car", "kind": "visit",
           "identity": {"name": "Ana", "kind": "vehicle"},
           "started_at": "2026-09-03T15:44:44+00:00",
           "ended_at": "2026-09-03T15:45:05+00:00", "duration_s": 21.0}
    walk = {"camera": cam, "class_name": "person", "kind": "visit",
            "started_at": "2026-09-03T15:45:30+00:00",
            "ended_at": "2026-09-03T15:45:52+00:00", "duration_s": 22.0}

    out = fold_arrival_walk([car, walk])
    assert len(out) == 1, "one arrival, one tile"
    assert out[0]["started_at"] == "2026-09-03T15:44:44+00:00", "opens on the car"
    assert out[0]["ended_at"] == "2026-09-03T15:45:52+00:00", "closes on the walk"
    assert out[0]["duration_s"] == 68.0

    # Somebody who was there BEFORE the car is not somebody who got out of it.
    early = {**walk, "started_at": "2026-09-03T15:44:00+00:00",
             "ended_at": "2026-09-03T15:44:20+00:00"}
    assert len(fold_arrival_walk([{**car}, early])) == 2

    # Nor is somebody on another camera.
    elsewhere = {**walk, "camera": {"id": "cam-2", "slug": "shed", "name": "Shed"}}
    assert len(fold_arrival_walk([{**car}, elsewhere])) == 2


def test_the_repair_asks_the_same_question_as_the_gate():
    """`claim_place` refuses a fill with no vehicle behind it. Until 05.09
    `open_missing_episodes` opened an episode for that same place thirty
    seconds later on no condition but the views, so every refusal was undone —
    a gate reversed on the next tick is not a gate."""
    import inspect

    from baba_core import occupancy

    repair = inspect.getsource(occupancy.open_missing_episodes)
    claim = inspect.getsource(occupancy.claim_place)
    assert "vehicle_behind_place_sql" in repair, (
        "the repair opens episodes without asking whether a car was there")
    assert "vehicle_behind_place_sql" in claim
    assert "_CLAIM_WINDOW" in repair and "_CLAIM_WINDOW" in claim, (
        "the two ask the same question through different windows")


def test_the_vehicle_predicate_numbers_its_parameters_from_where_it_is_told():
    from baba_core.occupancy import vehicle_behind_place_sql

    sql = vehicle_behind_place_sql(first_param=1)
    assert "$1" in sql and "$2" in sql and "$3" in sql and "$4" in sql
    shifted = vehicle_behind_place_sql(first_param=5)
    assert "$5" in shifted and "$8" in shifted and "$1" not in shifted
