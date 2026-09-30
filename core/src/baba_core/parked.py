"""Which named vehicle stands in which place — read from the registry.

This used to derive the answer on every call, pairing each occupied place with
whichever named vehicle's track had ended nearest that place's `present`
timestamp. It was the only thing that could be done before a track ended when
the car parked: nothing recorded WHO took a spot, so every reader had to guess.

The guess failed the way guesses do. Measured 2026-07-29: the Shed view of P1
had been stuck at `present` since the 27th, so "nearest track end" reached back
two days and picked the other household car — P1 was published as Ana's while
Marko's stood in it, with no corrupt data anywhere and no error to see. The same
call would have given a different answer an hour later.

Occupancy is now written when it happens (see baba_core.occupancy), so this is
a lookup. An episode with no identity stays nameless here rather than being
filled in by inference — the badge showing a place as occupied-but-unnamed is
the honest rendering of what the system knows. An episode named by a read
whose plate nobody has enrolled shows the raw registration: "ZG-9906-KB is
parked in P2" is exactly what the system knows about an unknown visitor.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from baba_core.occupancy import DEPARTURE_BOOKEND_S

__all__ = [
    "ParkedVehicle",
    "episode_departure_row",
    "episode_for",
    "parked_assignments",
]


@dataclass(frozen=True)
class ParkedVehicle:
    place: str
    # Empty when the spot is taken by a car nobody has identified. The badge
    # renders the place as occupied rather than disappearing: "something is
    # parked in P1" is a fact worth showing, and it is the honest rendering
    # while the plate has not been read.
    name: str
    since: datetime
    camera_ids: tuple[str, ...]


async def parked_assignments(pool: Any) -> list[ParkedVehicle]:
    """Open occupancy episodes, with every camera that views the place so the
    Live badge can render on each tile."""
    rows = await pool.fetch(
        """
        SELECT o.place, o.occupied_since,
               -- The raw registration renders only while the read still
               -- stands as evidence: a demoted episode keeps its read as
               -- history, and history is not a badge.
               COALESCE(il.name, CASE WHEN o.evidence = 'plate'
                                      THEN pr.plate_text END) AS name,
               array_agg(sr.camera_id::text) AS camera_ids
        FROM place_occupancy o
        LEFT JOIN identity_labels il ON il.global_id = o.global_id
        LEFT JOIN plate_reads pr ON pr.id = o.plate_read_id
        LEFT JOIN scene_regions sr ON sr.place = o.place AND sr.enabled
        WHERE o.released_at IS NULL
        GROUP BY o.place, o.occupied_since, il.name, o.evidence, pr.plate_text
        """
    )
    return [
        ParkedVehicle(
            place=r["place"],
            name=r["name"] or "",
            since=r["occupied_since"],
            camera_ids=tuple(c for c in (r["camera_ids"] or []) if c),
        )
        for r in rows
    ]


# How far from a departure a place may have emptied and still be that
# departure. Generous: the track dies when the car leaves the FRAME, while the
# spot is only re-evaluated on the evaluator's own interval.
VEHICLE_CLASSES = frozenset({"car", "truck", "bus", "motorcycle"})

_DEPARTURE_MATCH_S = 600.0


def episode_for(
    episodes: list[dict[str, Any]] | None, at: datetime, camera_id: str | None
) -> dict[str, Any] | None:
    """The closed episode this departure ends.

    Time alone is too weak — two spots can empty within the same window — so
    the camera has to be one that actually watches the place. A visit on a
    camera with no view of P2 cannot be P2's departure.
    """
    if not episodes:
        return None
    best = None
    for ep in episodes:
        if camera_id is not None and camera_id not in (ep["cameras"] or []):
            continue
        gap = abs((ep["released_at"] - at).total_seconds())
        if gap <= _DEPARTURE_MATCH_S and (best is None or gap < best[0]):
            best = (gap, ep)
    return best[1] if best else None


def _view_of(ep: dict[str, Any], prefer: str | None) -> dict[str, Any] | None:
    """The camera this row is shown and played from.

    The one the operator asked for when it watches this place, so a feed
    filtered to West never renders a row labelled Shed and never plays Shed's
    footage; otherwise the first view, and none at all when no camera watches
    the place — a row nothing can play is worse than no row.
    """
    views = ep.get("views") or []
    if not isinstance(views, list):
        return None
    named = [v for v in views if isinstance(v, dict) and v.get("id")]
    if prefer is not None:
        return next((v for v in named if v["id"] == prefer), None)
    return named[0] if named else None


def episode_departure_row(
    ep: dict[str, Any], prefer_camera: str | None = None
) -> dict[str, Any] | None:
    """A departure nobody's track witnessed, told by the registry alone.

    Measured over thirty days: every departure from P1 produced no identified
    vehicle track, and several produced no track at all — so the feed simply
    had no row for a car that demonstrably left a spot it had held for hours.
    """
    left = ep["released_at"]
    # Only a caller that did not select the registry's window lands here.
    from_ = ep.get("departure_from") or left - timedelta(
        seconds=DEPARTURE_BOOKEND_S)
    # A place is not a camera, but every row in this feed is played from one —
    # so the row carries a camera that actually watches the spot, and there is
    # no row at all when none does. An empty id here reached UUID() downstream
    # and took the whole feed down with a 500.
    cam = _view_of(ep, prefer_camera)
    if cam is None:
        return None
    return {
        "id": f"dep:{ep['place']}:{int(left.timestamp())}",
        "camera": {"id": cam.get("id", ""), "slug": cam.get("slug", ""),
                   "name": cam.get("name") or ep["place"]},
        "global_id": str(ep["global_id"]) if ep["global_id"] else "",
        "identity": {"name": ep["name"], "kind": ep["kind"] or "vehicle"} if ep["name"] else None,
        "class_name": "car",
        # Where the clip starts is the registry's, computed once by
        # `departure_window_sql` and carried on the episode — this feed used to
        # answer it for itself with a shorter number, so the operator watched a
        # clip that began on a car already reversing while the fix sat in the
        # announcement nobody looks at.
        "started_at": from_.isoformat(),
        "ended_at": left.isoformat(),
        "duration_s": (left - from_).total_seconds(),
        "track_count": 0,
        "observations": 0,
        "face_verified": False,
        "plate_text": None,
        "thumbnail_path": None,
        # What the plate reader saw as it went past, when it read one. Without
        # it this row is a black tile the operator cannot check against.
        "crop_path": ep.get("crop_path"),
        "arrival_thumbnail": None,
        "departure_thumbnail": None,
        "track_ids": [],
        "kind": "departure",
        "vehicle_name": None,
        "visit_duration_s": (left - ep["occupied_since"]).total_seconds(),
        "zones": [],
        "recording": None,
    }


def row_covering(feed: list[dict[str, Any]], lwv: dict[str, Any]) -> dict | None:
    """The vehicle row this person's departure belongs to, if it is here.

    Same camera and OVERLAPPING in time, not containing an instant: the two
    describe one departure from either end, and they stop at different moments
    — the registry closes when the place reads empty, the car clears the frame
    seconds after. Requiring containment left them as two tiles for one thing,
    with the operator playing whichever he clicked and seeing half of it.
    """
    a0 = datetime.fromisoformat(lwv["started_at"])
    a1 = datetime.fromisoformat(lwv["ended_at"])
    for r in feed:
        if r["camera"]["id"] != lwv["camera"]["id"]:
            continue
        if r.get("class_name") not in VEHICLE_CLASSES:
            continue
        if (datetime.fromisoformat(r["started_at"]) <= a1
                and datetime.fromisoformat(r["ended_at"]) >= a0):
            return r
    return None


# A gap in the picture that still reads as one continuous happening. The same
# number the feed already uses to keep a person briefly lost behind a tree as
# one visit, because it is the same question: somebody parks, sits for a moment
# and gets out, and that pause is not a second event. Measured on Ana's
# arrival of 03.09 it was twenty-five seconds; on her departure the mirror pause
# was thirty-two.
ARRIVAL_SETTLE = timedelta(minutes=2)


def fold_arrival_walk(feed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A car driving in and whoever got out of it walking off is ONE clip.

    The mirror of a departure, and it arrived split the same way: west had
    Ana's car from 17:44:44 to 17:45:05 and her walk from 17:45:30 to
    17:45:52, two tiles, neither showing the thing whole. So a vehicle row
    absorbs the run of person rows that follows it on the same camera, and the
    tile that comes out opens as the car enters the frame and closes as she
    leaves it.

    Only forwards, and only after the car has stopped: this is somebody getting
    OUT. A departure is joined at the other end, by `row_covering` — and once it
    is, the walk INTO it lies inside the vehicle row's window, so the second
    pass here drops that tile too.

    What it cannot tell apart is the driver getting out and a neighbour walking
    past a minute later — the first person after the car is taken to be the one
    who was in it. On this parcel that is who it is.
    """
    absorbed: set[int] = set()
    people = sorted(
        (r for r in feed if r.get("class_name") == "person"),
        key=lambda r: r["started_at"],
    )
    for v in feed:
        if v.get("class_name") not in VEHICLE_CLASSES:
            continue
        if v.get("kind") not in ("arrival", "visit"):
            continue
        opens = datetime.fromisoformat(v["started_at"])
        run_end = datetime.fromisoformat(v["ended_at"])
        for p in people:
            if id(p) in absorbed or p["camera"]["id"] != v["camera"]["id"]:
                continue
            start = datetime.fromisoformat(p["started_at"])
            if start < opens or start - run_end > ARRIVAL_SETTLE:
                continue
            run_end = max(run_end, datetime.fromisoformat(p["ended_at"]))
            absorbed.add(id(p))
        if run_end > datetime.fromisoformat(v["ended_at"]):
            v["ended_at"] = run_end.isoformat()
            v["duration_s"] = (run_end - opens).total_seconds()
    # And whoever the car's own clip already shows: the walk INTO a departure
    # sits inside that row's window from the moment it was joined at the other
    # end, and a second tile for eleven seconds of the same footage is noise
    # beside the row carrying the name and the plate. Same camera only — the
    # next camera's view of the same walk is its own thing to watch.
    for v in feed:
        if v.get("class_name") not in VEHICLE_CLASSES:
            continue
        opens = datetime.fromisoformat(v["started_at"])
        closes = datetime.fromisoformat(v["ended_at"])
        for p in people:
            if id(p) in absorbed or p["camera"]["id"] != v["camera"]["id"]:
                continue
            if (opens <= datetime.fromisoformat(p["started_at"])
                    and datetime.fromisoformat(p["ended_at"]) <= closes):
                absorbed.add(id(p))
    return [r for r in feed if id(r) not in absorbed]
