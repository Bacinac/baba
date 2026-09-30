"""The Activity feed: tracks become visits, visits claim the departures they
witnessed, and what nobody witnessed still appears — under the same filters.
"""

from datetime import UTC, datetime, timedelta

from baba_api.routes_sightings import (
    _claim_departure,
    _cluster_visits,
    _merge_left_with_vehicle,
    _unclaimed_departures,
    _zone_dwell,
    _zone_events_by_sighting,
)

T0 = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
PATIO, WEST = "cam-patio", "cam-west"
PERSON, CAR = 0, 2


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def track(n, start, end, *, cam=PATIO, cls=PERSON, name=None, obs=10, gid=None):
    return {
        "id": n,
        "camera_id": cam,
        "camera_slug": cam,
        "camera_name": cam,
        "class_id": cls,
        "class_name": "person" if cls == PERSON else "car",
        "started_at": at(start),
        "ended_at": at(end),
        "face_verified": False,
        "plate_text": None,
        "n_observations": obs,
        "identity_name": name,
        "identity_kind": "person" if name else None,
        "global_id": gid or f"gid-{n}",
        "thumbnail_path": f"t{n}.jpg",
        "crop_path": None,
    }


def spans(sightings):
    return sorted(
        (s["camera"]["id"], s["class_name"], s["track_ids"]) for s in sightings
    )


def test_tracks_close_in_time_on_one_camera_are_one_visit():
    rows = [  # DESC, the way the query returns them
        track(4, 20, 21),
        track(3, 5, 9),
        track(2, 1, 8),
        track(1, 0, 2),
    ]
    assert spans(_cluster_visits(rows)) == [
        (PATIO, "person", ["1", "2", "3"]),
        (PATIO, "person", ["4"]),
    ]


def test_a_visit_is_split_by_camera_and_by_class_group_never_by_identity():
    rows = [
        track(3, 1, 2, cam=WEST),
        track(2, 1, 2, cls=CAR),
        track(1, 0, 3, name="Ana", gid="ana"),
        track(0, 0, 1, gid="anon"),
    ]
    got = {tuple(s["track_ids"]): s for s in _cluster_visits(rows)}
    assert set(got) == {("0", "1"), ("2",), ("3",)}
    assert got[("0", "1")]["identity"] == {"name": "Ana", "kind": "person"}


def test_dwell_sums_the_time_inside_not_the_span():
    first, last, dwell = _zone_dwell([
        (at(0), "zone_enter"),
        (at(10), "zone_enter"),
        (at(30), "zone_exit"),
        (at(40), "zone_exit"),
        (at(280), "zone_enter"),
        (at(281), "zone_exit"),
    ])
    assert (first, last, dwell) == (at(0), at(281), 31 * 60.0)


def test_zone_events_are_grouped_per_sighting_and_zone():
    s = {"id": "s1"}
    rows = [
        {"track_id": 1, "payload": '{"zone_id": "z", "zone_name": "Carport", "zone_kind": "watch"}',
         "kind": "zone_enter", "at": at(0)},
        {"track_id": 2, "payload": {"zone_id": "z"}, "kind": "zone_dwell", "at": at(1)},
        {"track_id": 2, "payload": {"zone_id": "z"}, "kind": "zone_exit", "at": at(2)},
        {"track_id": 1, "payload": {}, "kind": "zone_enter", "at": at(3)},
        {"track_id": 9, "payload": {"zone_id": "z"}, "kind": "zone_enter", "at": at(4)},
    ]
    got = _zone_events_by_sighting(rows, {"1": s, "2": s})
    assert got == {
        ("s1", "z"): {
            "zone_id": "z",
            "zone_name": "Carport",
            "zone_kind": "watch",
            "events": [(at(0), "zone_enter"), (at(2), "zone_exit")],
        }
    }


def episode(place="P2", released=5, *, cameras=(WEST,), name="Ana", gid="car-ana", views=None):
    return {
        "place": place,
        "released_at": at(released),
        "occupied_since": at(released - 120),
        "cameras": list(cameras),
        "name": name,
        "kind": "vehicle" if name else None,
        "global_id": gid,
        "views": views if views is not None else [{"id": c, "slug": c, "name": c} for c in cameras],
    }


def row(kind="visit", *, cls="car", end=5.5, start=5.0, cam=WEST):
    return {
        "kind": kind,
        "class_name": cls,
        "camera": {"id": cam},
        "started_at": at(start).isoformat(),
        "ended_at": at(end).isoformat(),
        "identity": None,
        "global_id": "",
    }


def test_a_car_seen_leaving_claims_the_episode_and_carries_its_name():
    eps = [episode()]
    claimed = set()
    r = row()
    _claim_departure(r, eps, WEST, claimed)
    assert claimed == {("P2", int(at(5).timestamp()))}
    assert (r["kind"], r["visit_duration_s"]) == ("departure", 120 * 60.0)
    assert (r["identity"], r["global_id"]) == ({"name": "Ana", "kind": "vehicle"}, "car-ana")


def test_only_a_vehicle_on_a_camera_that_watches_the_place_claims_it():
    eps = [episode()]
    claimed = set()
    person, elsewhere = row(cls="person"), row(cam=PATIO)
    _claim_departure(person, eps, WEST, claimed)
    _claim_departure(elsewhere, eps, PATIO, claimed)
    assert claimed == set()
    assert person["kind"] == elsewhere["kind"] == "visit"


def test_a_split_departure_claims_without_being_rewritten():
    claimed = set()
    r = row("departure")
    _claim_departure(r, [episode()], WEST, claimed)
    assert len(claimed) == 1
    assert r["identity"] is None


def test_unwitnessed_departures_obey_the_feeds_filters():
    eps = [
        episode("P1", 1, cameras=(WEST, PATIO)),
        episode("P2", 5),
        episode("P3", 50),
        episode("P4", 6, name=None, gid=None),
    ]
    claimed = {("P2", int(at(5).timestamp()))}

    def places(**kw):
        args = {"want_cam": None, "until": None, "identity": None, "class_name": None} | kw
        return [r["id"].split(":")[1] for r in _unclaimed_departures(eps, claimed, **args)]

    assert places() == ["P1", "P3", "P4"]
    assert places(until=at(50)) == ["P1", "P4"]
    assert places(want_cam=PATIO) == ["P1"]
    assert places(identity="car-ana") == ["P1", "P3"]
    assert places(class_name="person") == []
    assert places(class_name="truck") == ["P1", "P3", "P4"]
    (patio,) = _unclaimed_departures(eps, claimed, want_cam=PATIO, until=None, identity=None, class_name=None)
    assert patio["camera"]["id"] == PATIO


def test_a_departure_no_camera_can_play_is_no_row():
    assert _unclaimed_departures([episode(views=[])], set(), want_cam=None, until=None,
                                 identity=None, class_name=None) == []


def test_a_person_leaving_by_car_rides_on_the_cars_row():
    car = row(start=5.0, end=6.0) | {"duration_s": 60.0}
    walk = row(cls="person", start=3.0, end=5.5) | {"identity": {"name": "Ana"}}
    feed = [car]
    _merge_left_with_vehicle(feed, walk)
    assert feed == [car]
    assert (car["started_at"], car["ended_at"]) == (at(3).isoformat(), at(6).isoformat())
    assert (car["duration_s"], car["left_with"]) == (180.0, "Ana")


def test_a_person_leaving_with_no_car_row_gets_a_row_of_their_own():
    walk = row(cls="person", start=3.0, end=5.5)
    feed = [row(start=10.0, end=11.0)]
    _merge_left_with_vehicle(feed, walk)
    assert feed[-1] is walk
