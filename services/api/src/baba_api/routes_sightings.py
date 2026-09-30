"""Sightings — UI-level aggregation of tracks into continuous presences.

The Events feed (routes_events.py) lists one row per `track_finalized`,
which is the raw output of ByteTrack's lifetime ending. That's too
low-level for a typical operator: one car parked for 5 hours can
produce 20 tracks if ByteTrack briefly loses then re-acquires it, and
a single person crossing a frame through three zones produces one
track with three zone_enter/exit pairs but four event rows.

A `sighting` is the higher-level view: a VISIT — "something of this
class group was present on this camera continuously over this time
range". We cluster tracks per `(camera_id, class group)` collapsing
gaps shorter than `_GAP_TOLERANCE` so re-acquisitions stitch back
together; identity is an ATTRIBUTE of the visit (the named identity
with the most observations), never the clustering key — grouping by
global_id split one physical visit into overlapping entries whenever
identity resolution fragmented across its tracks. Zones visited
during the sighting are aggregated as metadata, not as separate rows.

Recording lookup is the same as for events — the LATERAL JOIN finds
the segment that covers the sighting's midpoint so the UI's PLAY
button can land in the right place.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from baba_core import group_for
from baba_core.classes import PERSON_CLASS_ID
from baba_core.occupancy import (
    DEPARTURE_BOOKEND_S,
    departure_window_sql,
    episode_witness_sql,
)
from baba_core.parked import (
    VEHICLE_CLASSES,
    episode_departure_row,
    episode_for,
    fold_arrival_walk,
    row_covering,
)
from baba_core.recordings import covers_until_sql
from fastapi import APIRouter, Query, Request

from baba_api.sqlfilter import SqlFilter

log = logging.getLogger(__name__)

sightings_router = APIRouter()

# Tracks of the same global_id less than this far apart are treated as
# one continuous presence. Tuned for residential CCTV: a person walks
# behind a tree for a few seconds and ByteTrack briefly loses them; we
# don't want that to register as two visits.
_GAP_TOLERANCE = timedelta(minutes=2)

# Clip bookends. A parked vehicle's middle is dead time — nobody wants a 4-hour
# clip of a car sitting still. So a LONG vehicle visit yields two short clips:
# the arrival (drive-in + settling) and the departure (start-up + drive-off),
# skipping the inert middle. A PERSON is never split, however still they go:
# their stillness is presence, not dead time (the whole reason we fought to keep
# the stationary person as one continuous visit). Everything short gets one clip.
# Gated on CLASS, not on how long it sat: only a vehicle's parked phase is
# genuinely skippable. object_parked/unparked events are too flappy to cut on
# (they oscillate every few seconds while the tracker settles), so the bookends
# are the first and last N seconds of the visit — robust and event-free.
_BOOKEND_S = 40.0
# Only split when the dead middle is worth skipping: the two 40 s bookends plus
# a real gap between them. Below this the visit is short enough to watch whole.
_SPLIT_MIN_S = 150.0

# How far from a visit's end a spot may empty and still be that visit's
# departure. Generous, because the track dies when the car leaves the FRAME
# while the spot is only re-evaluated on the evaluator's own interval.
_DEPARTURE_MATCH = timedelta(minutes=10)
# How far back registry departures reach when the caller named no window.
_EPISODE_DEFAULT_WINDOW = timedelta(days=2)


async def _parked_episodes(pool, since: datetime) -> list[dict[str, Any]]:
    """Closed occupancy episodes: who stood where, from when to when.

    The registry is the authority on who left. A departing car is a fresh
    moving track that may read no plate at all — measured over thirty days,
    every one of Marko's departures from P1 produced no identified track, while
    the episode it ended carried his name on plate evidence since the arrival.
    Naming the departure from the track was asking the weaker witness.
    """
    rows = await pool.fetch(
        f"""
        SELECT o.*,
               {departure_window_sql("o.released_at", "o.witness::uuid")}
                   AS departure_from
        FROM (
        SELECT o.place, o.occupied_since, o.released_at, o.global_id, o.evidence,
               -- A guest's raw registration is the name everywhere else (Live
               -- badge, analytics); the feed's departure rows say it too.
               COALESCE(il.name, CASE WHEN o.evidence = 'plate'
                                      THEN pr.plate_text END) AS name,
               il.kind,
               -- The crop the plate reader saved of the car going past. A
               -- departure nobody's track witnessed had `thumbnail_path: None`
               -- by construction, so it rendered as a black tile — a row that
               -- shows nothing is a row the operator cannot check.
               pr.crop_path,
               (SELECT array_agg(sr.camera_id::text) FROM scene_regions sr
                 WHERE sr.place = o.place) AS cameras,
               (SELECT jsonb_agg(jsonb_build_object('id', c.id::text,
                                                     'slug', c.slug,
                                                     'name', c.name)
                                  ORDER BY c.slug)
                  FROM (SELECT DISTINCT sr.camera_id FROM scene_regions sr
                         WHERE sr.place = o.place AND sr.enabled) v
                  JOIN cameras c ON c.id = v.camera_id) AS views,
               -- The view that actually watched it go. Not this feed's own
               -- opinion any more: the same expression the announcement uses,
               -- because the departure window is computed FROM the witness and
               -- two different witnesses gave the bus and the tile moments up
               -- to a minute apart on 6 of 25 closed episodes.
               {episode_witness_sql("o.place", "pr.camera_id")}::text AS witness
        FROM place_occupancy o
        LEFT JOIN identity_labels il ON il.global_id = o.global_id
        LEFT JOIN plate_reads pr ON pr.id = o.plate_read_id
        WHERE o.released_at IS NOT NULL AND o.released_at >= $1
        ) o
        -- Where the clip starts is the registry's answer, not this feed's: the
        -- same expression the announcement uses, so the row an operator plays
        -- and the event that reaches the bus cannot describe different moments.
        ORDER BY o.released_at
        """,  # noqa: S608
        since,
    )
    out = []
    for r in rows:
        ep = dict(r)
        # asyncpg hands jsonb back as text unless a codec is registered.
        if isinstance(ep.get("views"), str):
            ep["views"] = json.loads(ep["views"])
        out.append(ep)
    return out


async def _parking_departures(
    pool, since: datetime
) -> list[tuple[datetime, datetime | None]]:
    """Occupancy intervals of the parking places, from the registry.

    This used to replay `scene_state_change` events with per-view state held
    as of each moment — a second derivation of the very intervals the
    registry records from the same transitions (first view to notice fills,
    last view to let go empties, judged at that moment). Two derivations of
    one fact eventually disagree, and the one facing the operator is always
    the one that is wrong.

    Returns (emptied_at, filled_at) pairs, oldest first.
    """
    rows = await pool.fetch(
        """
        SELECT occupied_since, released_at
        FROM place_occupancy
        WHERE released_at IS NOT NULL AND released_at >= $1
        ORDER BY released_at
        """,
        since,
    )
    return [(r["released_at"], r["occupied_since"]) for r in rows]


def _expand_visit(
    v: dict[str, Any],
    departures: list[tuple[datetime, datetime | None]] | None = None,
    episodes: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """A visit becomes one feed entry, or two for a long vehicle.

    A parked vehicle's arrival and departure happen HOURS apart, so each is its
    own feed row AT THE TIME IT HAPPENED — the departure surfaces fresh at 09:11
    instead of being buried on the 04:52 arrival row. Each row's window is a
    short bookend (drive-in+settle, wake+drive-off); the dead parked middle is
    never a row and never a clip. A PERSON is never split, however still they
    go — their arrival and departure are the same moment (they pass through) or
    their stillness is presence, not two events. One row, one clip.

    Each row's `started_at`/`ended_at` IS its clip window, so it sorts and plays
    at its own moment.

    The departure row only exists when a parking spot actually emptied near the
    end of the visit. Without that corroboration the vehicle is still standing
    where it parked and only its track ended, so the visit stays a single row
    rather than announcing a departure that never happened.
    """
    started = datetime.fromisoformat(v["started_at"])
    ended = datetime.fromisoformat(v["ended_at"])
    left_at: datetime | None = None
    parked_since: datetime | None = None
    if departures is not None:
        for at, since_at in departures:
            if started <= at <= ended + _DEPARTURE_MATCH:
                left_at, parked_since = at, since_at
                break
    if (
        v["class_name"] in VEHICLE_CLASSES
        and (ended - started).total_seconds() > _SPLIT_MIN_S
        and (departures is None or left_at is not None)
    ):
        # How long it stood: the spot's own occupied interval when we have it,
        # so both cameras' rows agree, and the track's length only when no
        # region was watching.
        stood_s = (
            (left_at - parked_since).total_seconds()
            if left_at and parked_since
            else v["duration_s"]
        )
        arrival = {**v, "id": v["id"] + ":arr", "kind": "arrival",
                   "visit_duration_s": stood_s, "duration_s": _BOOKEND_S,
                   "thumbnail_path": v["arrival_thumbnail"],
                   "started_at": v["started_at"],
                   "ended_at": (started + timedelta(seconds=_BOOKEND_S)).isoformat()}
        # Anchor the departure clip on the moment the spot emptied when we have
        # it; the track's own end is only a fallback for cameras with no
        # parking region watching them.
        dep_end = left_at or ended
        # Both rows are about the car that STOOD there, and the registry is who
        # knows that. The tracks either end of a parked spell are two different
        # views of one visit and the departing one is the poorer witness — it
        # often reads no plate at all.
        ep = episode_for(episodes, dep_end, v["camera"]["id"])
        # And the registry knows where the departure begins, too: the walk to
        # the car, computed once by `departure_window_sql`. This row is the one
        # an operator actually plays — it carries the picture and the plate —
        # and it used to cut its own forty seconds, which opened on a car
        # already reversing. Only a camera with no parking region watching it
        # falls back to a bookend.
        dep_from = (ep or {}).get("departure_from") or (
            dep_end - timedelta(seconds=DEPARTURE_BOOKEND_S))
        departure = {**v, "id": v["id"] + ":dep", "kind": "departure",
                     "visit_duration_s": stood_s,
                     "duration_s": (dep_end - dep_from).total_seconds(),
                     "thumbnail_path": v["departure_thumbnail"],
                     "started_at": dep_from.isoformat(),
                     "ended_at": dep_end.isoformat()}
        if ep is not None and ep["name"]:
            named = {"name": ep["name"], "kind": ep["kind"] or "vehicle"}
            arrival["identity"] = departure["identity"] = named
            # Empty for a guest named by raw registration — "None" as a
            # string once reached UUID() downstream.
            gid = str(ep["global_id"]) if ep["global_id"] else ""
            arrival["global_id"] = departure["global_id"] = gid
            if ep["evidence"] == "plate":
                arrival["face_verified"] = departure["face_verified"] = False
        return [arrival, departure]
    return [{**v, "kind": "visit", "visit_duration_s": v["duration_s"]}]


def _aware(at: datetime | None) -> datetime | None:
    if at is not None and at.tzinfo is None:
        return at.replace(tzinfo=UTC)
    return at


@sightings_router.get("/tracks/suppressed")
async def list_suppressed_tracks(
    request: Request,
    camera_id: UUID | None = Query(default=None),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=2000),
) -> list[dict[str, Any]]:
    """Tracks the pipeline followed and the zone or static filter kept out of
    the timeline, newest-first.

    These rows carry no thumbnail and no identity — the filter exists to skip
    that work — so they answer one question only, and it is the question that
    used to need the recording: did this camera see anything here, and what
    hid it."""
    pool = request.app.state.pool
    flt = SqlFilter("t.suppressed_reason IS NOT NULL")
    if camera_id is not None:
        flt.add("t.camera_id = ?", camera_id)
    if since is not None:
        flt.add("t.ended_at >= ?", _aware(since))
    if until is not None:
        flt.add("t.started_at < ?", _aware(until))
    limit_param = flt.bind(limit)
    sql = f"""
        SELECT t.id, t.camera_id, t.class_name, t.started_at, t.ended_at,
               t.n_observations, t.suppressed_reason,
               c.slug AS camera_slug, c.name AS camera_name
        FROM tracks_all t
        JOIN cameras c ON c.id = t.camera_id
        {flt.where(required=True)}
        ORDER BY t.started_at DESC
        LIMIT {limit_param}
    """  # noqa: S608
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *flt.args)
    return [
        {
            "id": str(r["id"]),
            "camera": {
                "id": str(r["camera_id"]),
                "slug": r["camera_slug"],
                "name": r["camera_name"],
            },
            "class_name": r["class_name"],
            "started_at": r["started_at"].isoformat(),
            "ended_at": r["ended_at"].isoformat(),
            "observations": r["n_observations"],
            "suppressed_reason": r["suppressed_reason"],
        }
        for r in rows
    ]


async def _fetch_confirmed_tracks(
    pool,
    limit: int,
    *,
    camera_id: UUID | None,
    since: datetime | None,
    until: datetime | None,
    class_name: str | None,
    identity: UUID | None,
) -> list[Any]:
    # Fetch a window of tracks, then cluster client-side in Python.
    # The cluster step is O(N) over the rows after the SQL sort, so
    # this stays cheap even at the default 5000-track upper bound.
    flt = SqlFilter("t.global_id IS NOT NULL")

    if camera_id is not None:
        flt.add("t.camera_id = ?", camera_id)
    if since is not None:
        flt.add("t.ended_at >= ?", since)
    if until is not None:
        flt.add("t.started_at < ?", until)
    if class_name is not None:
        flt.add("t.class_name = ?", class_name)
    if identity is not None:
        flt.add("t.global_id = ?", identity)

    # Fetch a reasonable supply of recent tracks so the clustering has
    # something to chew on. `limit * 5` is a heuristic — most clusters
    # are 1–3 tracks long, so 5× gives the SQL enough headroom to feed
    # `limit` distinct clusters in the typical case.
    sql_limit = limit * 5
    limit_param = flt.bind(sql_limit)

    where = flt.where(required=True)
    sql = f"""
        SELECT
            t.id, t.camera_id, t.global_id, t.class_id, t.class_name,
            t.started_at, t.ended_at, t.thumbnail_path, t.crop_path,
            t.face_verified, t.n_observations, t.plate_text,
            c.slug AS camera_slug, c.name AS camera_name,
            l.name AS identity_name, l.kind AS identity_kind
        FROM tracks t
        JOIN cameras c ON c.id = t.camera_id
        LEFT JOIN identity_labels l ON l.global_id = t.global_id
        {where}
        -- A track earns a row by having been CONFIRMED as something, not by
        -- lasting a long time. Measured over seven days: 325 tracks produced no
        -- embedding sample at all and not one of them was ever face-verified,
        -- while the largest of them held a phantom on a night patio for 17
        -- minutes and 34,397 observations — furniture and a fan. Duration and
        -- observation count prove nothing; a thing that never moves accumulates
        -- more of both than a person walking past.
        --
        -- So a sample (the embedder accepted a crop), a name, or a face. What
        -- fails this is not deleted — it stays in the database, the telemetry
        -- and the recordings — it simply is not an event. Costs 45% of rows on
        -- the camera pointed at a washing line and under 6% everywhere else.
          AND (
              t.face_verified
              OR l.name IS NOT NULL
              OR EXISTS (
                  SELECT 1 FROM track_embedding_samples s WHERE s.track_id = t.id
              )
          )
        -- And a VEHICLE track additionally has to have moved at some point.
        -- A car's stillness, unlike a person's, is not presence worth a row:
        -- the arrival row already said it came, the place says it is still
        -- there, and the departure row will say when it left. What a
        -- never-moved vehicle track records is the tracker re-finding a
        -- standing car — twenty times in one measured night — and each
        -- re-find carries a sample, so the evidence gate alone lets it
        -- through. NULL passes: history predating the column.
          AND (
              t.class_name NOT IN ('car', 'truck', 'bus', 'motorcycle')
              OR t.ever_active IS DISTINCT FROM false
          )
        ORDER BY t.started_at DESC
        LIMIT {limit_param}
    """  # noqa: S608
    return await pool.fetch(sql, *flt.args)


def _cluster_visits(rows: list[Any]) -> list[dict[str, Any]]:
    """A VISIT is a per-camera, per-class-group TIME span — not a
    per-identity one. Grouping by global_id (the old key) split one physical
    visit into parallel entries whenever identity resolution fragmented
    across its tracks (live: one patio stay = 4 tracks under 3 gids → 3
    overlapping Activity clips). Tracks of the same class group on the same
    camera that touch or sit within _GAP_TOLERANCE of each other are ONE
    sighting; identity is an attribute of the visit (picked in
    _build_sighting), never the clustering key. Identity-filtered calls are
    unaffected: their rows are pre-filtered to one identity, so time
    clustering yields exactly the per-identity runs it always did."""
    by_key: dict[tuple[Any, Any], list[Any]] = {}
    for r in rows:
        key = (r["camera_id"], group_for(r["class_id"]))
        by_key.setdefault(key, []).append(r)

    sightings: list[dict[str, Any]] = []
    for tracks in by_key.values():
        # Tracks come back DESC; flip to ASC for run-detection. Overlapping
        # tracks (double-tracking) make the LAST member's end unreliable, so
        # stitch against the running MAX end of the run.
        run: list[Any] = []
        run_end: datetime | None = None
        for tr in reversed(tracks):
            if run and run_end is not None and (tr["started_at"] - run_end) > _GAP_TOLERANCE:
                sightings.append(_build_sighting(run))
                run = []
                run_end = None
            run.append(tr)
            run_end = tr["ended_at"] if run_end is None else max(run_end, tr["ended_at"])
        if run:
            sightings.append(_build_sighting(run))
    return sightings


def _claim_departure(
    r: dict[str, Any], episodes, camera_id: str, claimed: set[tuple[str, int]]
) -> None:
    """A departure is ONE happening, and it belongs on the row that can
    show it. Ana drove off P2 at 09:12 on 31.08 and the feed carried
    three rows for it: the car's own 28-second sighting with the plate
    in the thumbnail, the registry's "the spot emptied" and the
    person's "left by car" — the last two blank tiles reading `0
    tracks · 0 obs`. Only a SPLIT vehicle row used to claim the
    episode, and a 28-second sighting is far too short to split, so
    the registry never learned it had a witness.

    So any vehicle seen at the right moment on a camera that watches
    the place claims it, and carries what the registry knows: who it
    was and how long it stood."""
    if r["kind"] not in ("departure", "visit") or r["class_name"] not in VEHICLE_CLASSES:
        return
    ep = episode_for(episodes, datetime.fromisoformat(r["ended_at"]), camera_id)
    if ep is None:
        return
    claimed.add((ep["place"], int(ep["released_at"].timestamp())))
    if r["kind"] != "visit":
        return
    r["kind"] = "departure"
    r["visit_duration_s"] = (ep["released_at"] - ep["occupied_since"]).total_seconds()
    if ep["name"]:
        r["identity"] = {"name": ep["name"], "kind": ep["kind"] or "vehicle"}
        r["global_id"] = str(ep["global_id"]) if ep["global_id"] else ""


def _unclaimed_departures(
    episodes,
    claimed: set[tuple[str, int]],
    *,
    want_cam: str | None,
    until: datetime | None,
    identity: UUID | None,
    class_name: str | None,
) -> list[dict[str, Any]]:
    """A spot that emptied with no track to tell it still emptied — but it
    obeys the same filters as every other row in this feed. Emitted
    unfiltered, a departure from a place West watches turned up in a feed
    filtered to Patio and played West's footage."""
    if class_name is not None and class_name not in VEHICLE_CLASSES:
        return []
    out: list[dict[str, Any]] = []
    for ep in episodes or []:
        if (ep["place"], int(ep["released_at"].timestamp())) in claimed:
            continue
        if until is not None and ep["released_at"] >= until:
            continue
        if want_cam is not None and want_cam not in (ep["cameras"] or []):
            continue
        if identity is not None and str(identity) != str(ep["global_id"] or ""):
            continue
        row = episode_departure_row(ep, want_cam or ep.get("witness"))
        if row is not None:
            out.append(row)
    return out


def _merge_left_with_vehicle(feed: list[dict[str, Any]], lwv: dict[str, Any]) -> None:
    # The person walking to their car and the car pulling away are one
    # departure seen twice. When the car's own row is already in the feed at
    # that moment, the person rides ON it — the row with the picture and the
    # registration — instead of adding a second blank tile beside it.
    host = row_covering(feed, lwv)
    if host is None:
        feed.append(lwv)
        return
    # One tile, and it spans BOTH: the walk in from the person's row and the
    # drive-off from the car's. Playing it shows the departure whole rather
    # than whichever half the operator happened to click.
    start = min(host["started_at"], lwv["started_at"])
    end = max(host["ended_at"], lwv["ended_at"])
    host["started_at"], host["ended_at"] = start, end
    host["duration_s"] = (
        datetime.fromisoformat(end) - datetime.fromisoformat(start)
    ).total_seconds()
    if lwv.get("identity"):
        host["left_with"] = lwv["identity"]["name"]


@sightings_router.get("/sightings")
async def list_sightings(
    request: Request,
    camera_id: UUID | None = Query(default=None),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    class_name: str | None = Query(default=None, max_length=64),
    identity: UUID | None = Query(default=None, description="Filter by global_id"),
) -> list[dict[str, Any]]:
    """Return sightings newest-first. A sighting clusters tracks of the
    same global_id on the same camera into one continuous presence."""
    pool = request.app.state.pool
    # The window is compared against timestamps in Python now, not only handed
    # to SQL, and a caller may leave the zone off.
    since = _aware(since)
    until = _aware(until)

    rows = await _fetch_confirmed_tracks(
        pool,
        limit,
        camera_id=camera_id,
        since=since,
        until=until,
        class_name=class_name,
        identity=identity,
    )
    sightings = _cluster_visits(rows)

    # A long parked vehicle becomes two feed rows (arrival, departure), each at
    # its own moment; everything else stays one. Sort newest-first, cap.
    departures: list[tuple[datetime, datetime | None]] | None = None
    if sightings:
        departures = await _parking_departures(
            pool, min(datetime.fromisoformat(v["started_at"]) for v in sightings)
        )
    # Independently of the tracks, and over the window that was ASKED FOR. A
    # departure nobody's track witnessed is the whole reason this exists, and
    # both of those were wrong: bounded by the oldest surviving sighting it
    # dropped every earlier episode of a busy day, and fetched only when some
    # sighting existed it produced nothing on a quiet window whose one event
    # was a car leaving.
    episodes = await _parked_episodes(
        pool, since or (datetime.now(UTC) - _EPISODE_DEFAULT_WINDOW)
    )
    feed: list[dict[str, Any]] = []
    claimed: set[tuple[str, int]] = set()
    for v in sightings:
        expanded = _expand_visit(v, departures, episodes)
        for r in expanded:
            _claim_departure(r, episodes, v["camera"]["id"], claimed)
        feed.extend(expanded)
    want_cam = str(camera_id) if camera_id is not None else None
    feed.extend(
        _unclaimed_departures(
            episodes,
            claimed,
            want_cam=want_cam,
            until=until,
            identity=identity,
            class_name=class_name,
        )
    )
    # Punctual facts ride the same feed: a person leaving in their linked car
    # is a moment, not a visit, but it belongs where the operator reads the
    # day. The row starts where the walk to the car does, so PLAY shows the
    # walk-out and the drive-off instead of a driver already seated.
    for lwv in await _left_with_vehicle_rows(
        pool, camera_id, since, until, identity
    ):
        _merge_left_with_vehicle(feed, lwv)
    # A car driving in and whoever got out of it walking off is one clip, the
    # same way a walk out and a drive-off is. Folded after everything is in the
    # feed, so it sees the person rows too.
    feed = fold_arrival_walk(feed)
    feed.sort(key=lambda s: s["started_at"], reverse=True)
    sightings = feed[:limit]

    # Enrich each sighting with the recording segment covering its
    # midpoint so the UI's PLAY button can jump straight in.
    if sightings:
        await _attach_recordings(pool, sightings)
        await _attach_zones(pool, sightings)
    return sightings


# A person walking to a car and that car driving off is ONE thing to watch, and
# its edges are in the tracks rather than in any number: it opens on the first
# frame the person is in and closes when the car is out of shot.
#
# This joins the tracker's own FRAGMENTS of one walk — west cut Ana's crossing
# into two pieces meeting at the same second — and nothing else. It is
# deliberately not applied between the walk and the car: somebody gets in, belts
# up and pulls out, which on the same departure was thirty-two seconds of a
# parked car with nobody visible. Treating that pause as a break is what left
# the clip starting on the car.
_WALK_CONTINUITY = timedelta(seconds=30)
# Only bounds the scan: the walk is whatever ran up to the car within it.
_WALK_HORIZON = timedelta(minutes=10)
# What a departure gets when nothing says when the car started moving, and the
# shortest it may ever be. Same minute the registry's own bookend uses.
_DRIVE_OFF_FALLBACK = timedelta(seconds=DEPARTURE_BOOKEND_S)
# The longest one tile may span. The clip cutter re-encodes anything that is not
# H.264 and caps that path at four minutes; beyond it a clip comes back in
# pieces, so a window wider than this is a tile that cannot play as one thing.
_SPAN_MAX = timedelta(minutes=4)


async def _walk_to_drive_off(
    pool, rows: list[Any], payloads: list[dict[str, Any]]
) -> dict[Any, tuple[datetime, datetime]]:
    """For each departure-by-car, the span from the walk to the car clearing.

    The vehicle's own track gives the far edge — the event is filed at its end —
    and the near edge is the start of the unbroken run of person tracks leading
    into it. The person cannot be matched by identity: on west the walk is
    tracked anonymously and only the CAR carries the name, which is why this
    reaches for whoever was on that camera rather than for that global_id.
    """
    spans: dict[Any, tuple[datetime, datetime]] = {}
    for r, p in zip(rows, payloads, strict=True):
        veh = await pool.fetchrow(
            """
            SELECT started_at, ended_at FROM tracks
             WHERE camera_id = $1 AND global_id = $2::uuid AND ended_at = $3
             LIMIT 1
            """,
            r["camera_id"], p.get("vehicle_gid"), r["at"],
        )
        closes = veh["ended_at"] if veh else r["at"]
        # When the car BEGAN MOVING, which is not when its track began. A
        # parked car's track starts when it arrived: measured, one departure
        # inherited a track running 03:30 to 17:25 and the tile became 13.9
        # hours, sorted to half past three in the morning, and played four
        # minutes of an empty carport. `object_unparked` is the moment itself.
        unparked = await pool.fetchval(
            """
            SELECT max(at) FROM events
             WHERE camera_id = $1 AND kind = 'object_unparked'
               AND at <= $2 AND at > $2::timestamptz - $3::interval
            """,
            r["camera_id"], closes, _WALK_HORIZON,
        )
        opens = unparked or (closes - _DRIVE_OFF_FALLBACK)
        walk = await pool.fetch(
            f"""
            SELECT started_at, ended_at FROM tracks
             WHERE camera_id = $1 AND class_id = {PERSON_CLASS_ID}
               AND ended_at <= $2 AND ended_at > $3
             ORDER BY started_at DESC
            """,  # noqa: S608
            r["camera_id"], opens, opens - _WALK_HORIZON,
        )
        run = opens
        for t in walk:  # backwards from the last person before the car moved
            if run != opens and run - t["ended_at"] > _WALK_CONTINUITY:
                break
            run = min(run, t["started_at"])
        # Bounded at both ends, so no chain of tracks can turn one departure
        # into an afternoon. The far bound is the clip cutter's own re-encode
        # cap: past it a clip arrives in pieces, each with its own progress bar.
        run = max(run, closes - _SPAN_MAX)
        run = min(run, closes - _DRIVE_OFF_FALLBACK)
        spans[r["event_id"]] = (run, closes)
    return spans


async def _left_with_vehicle_rows(
    pool,
    camera_id: UUID | None,
    since: datetime | None,
    until: datetime | None,
    identity: UUID | None,
) -> list[dict[str, Any]]:
    """`left_with_vehicle` events shaped as feed rows."""
    flt = SqlFilter("e.kind = 'left_with_vehicle'")
    if camera_id is not None:
        flt.add("e.camera_id = ?", camera_id)
    if since is not None:
        flt.add("e.at >= ?", since)
    if until is not None:
        flt.add("e.at < ?", until)
    if identity is not None:
        flt.add("(e.payload->>'person_gid')::uuid = ?", identity)
    # One row per departure, however many presence episodes it closed: the
    # event-manager emits one event per closed stay, so one person leaving in
    # one car can produce several identical events in the same second.
    rows = await pool.fetch(
        f"""
        SELECT DISTINCT ON (e.payload->>'person_gid', e.payload->>'vehicle_gid', e.at)
               e.id AS event_id, e.at, e.payload,
               c.id AS camera_id, c.slug, c.name AS camera_name
        FROM events e JOIN cameras c ON c.id = e.camera_id
        {flt.where(required=True)}
        ORDER BY e.payload->>'person_gid', e.payload->>'vehicle_gid', e.at DESC
        LIMIT 50
        """,  # noqa: S608
        *flt.args,
    )
    if not rows:
        return []
    gids: set[str] = set()
    payloads = []
    for r in rows:
        p = r["payload"]
        p = json.loads(p) if isinstance(p, str) else p
        payloads.append(p)
        gids.update(g for g in (p.get("person_gid"), p.get("vehicle_gid")) if g)
    names = {
        str(n["global_id"]): n["name"]
        for n in await pool.fetch(
            "SELECT global_id, name FROM identity_labels WHERE global_id = ANY($1::uuid[])",
            list(gids),
        )
    }
    spans = await _walk_to_drive_off(pool, rows, payloads)
    out: list[dict[str, Any]] = []
    for r, p in zip(rows, payloads, strict=True):
        person = p.get("person_gid", "")
        out.append({
            # Keyed by the event's own uuid: camera+second collided the moment
            # two episodes closed in the same second, and a duplicated feed id
            # is a hard render error client-side.
            "id": f"lwv:{r['event_id']}",
            "camera": {"id": str(r["camera_id"]), "slug": r["slug"],
                       "name": r["camera_name"]},
            "global_id": person,
            "identity": (
                {"name": names[person], "kind": "person"}
                if person in names else None
            ),
            "class_name": "person",
            "started_at": spans[r["event_id"]][0].isoformat(),
            "ended_at": spans[r["event_id"]][1].isoformat(),
            "duration_s": (spans[r["event_id"]][1]
                           - spans[r["event_id"]][0]).total_seconds(),
            "track_count": 0,
            "observations": 0,
            "face_verified": False,
            "plate_text": None,
            "thumbnail_path": None,
            "crop_path": None,
            "arrival_thumbnail": None,
            "departure_thumbnail": None,
            "track_ids": [],
            "kind": "left_with_vehicle",
            "vehicle_name": names.get(p.get("vehicle_gid", "")),
            "visit_duration_s": (spans[r["event_id"]][1]
                                 - spans[r["event_id"]][0]).total_seconds(),
            "zones": [],
            "recording": None,
        })
    return out


def _build_sighting(run: list[Any]) -> dict[str, Any]:
    """Collapse a list of contiguous tracks into a single sighting row."""
    first = run[0]
    started_at: datetime = first["started_at"]
    # Overlapping members make the last track's end unreliable — the visit
    # ends when the LATEST member ends.
    ended_at: datetime = max(r["ended_at"] for r in run)
    # Pick the longest-lived track's thumbnail as the representative
    # one — longer tracks tend to have cleaner mid-frame shots.
    best = max(run, key=lambda r: (r["ended_at"] - r["started_at"]).total_seconds())
    # For a split vehicle: the FIRST track (earliest) shows the drive-in, the
    # LAST (latest end) shows the drive-off — not the parked mid-frame `best`.
    arrival_track = run[0]
    departure_track = max(run, key=lambda r: r["ended_at"])
    track_ids = [str(r["id"]) for r in run]
    class_counts: dict[str, int] = {}
    for r in run:
        class_counts[r["class_name"]] = class_counts.get(r["class_name"], 0) + 1
    majority_class = max(class_counts.items(), key=lambda kv: kv[1])[0]
    any_face_verified = any(r["face_verified"] for r in run)
    # The plate a track actually read, if any. Reported separately from
    # face/body because it is neither: it is the one identifier on a vehicle
    # that is not appearance, and folding it into "recognised by body" told the
    # operator the weakest thing the system knows when it knew the strongest.
    plate = next((r["plate_text"] for r in run if r["plate_text"]), None)
    total_observations = sum(r["n_observations"] for r in run)
    # The visit's identity: the run may mix named and anonymous tracks
    # (identity fragments across a visit's tracks). The NAMED identity with
    # the most observations represents the visit; anonymous-only runs stay
    # unnamed.
    named = [r for r in run if r["identity_name"] is not None]
    primary = max(named, key=lambda r: r["n_observations"]) if named else first
    return {
        "id": f"sighting:{first['camera_id']}:{primary['global_id']}:{int(started_at.timestamp())}",
        "camera": {
            "id": str(first["camera_id"]),
            "slug": first["camera_slug"],
            "name": first["camera_name"],
        },
        "global_id": str(primary["global_id"]),
        "identity": (
            {"name": primary["identity_name"], "kind": primary["identity_kind"]}
            if primary["identity_name"] is not None
            else None
        ),
        "class_name": majority_class,
        "started_at": started_at.isoformat(),
        "ended_at": ended_at.isoformat(),
        "duration_s": (ended_at - started_at).total_seconds(),
        "track_count": len(run),
        "observations": total_observations,
        "face_verified": any_face_verified,
        "plate_text": plate,
        "thumbnail_path": best["thumbnail_path"],
        "crop_path": best["crop_path"],
        "arrival_thumbnail": arrival_track["thumbnail_path"] or arrival_track["crop_path"],
        "departure_thumbnail": departure_track["thumbnail_path"] or departure_track["crop_path"],
        "track_ids": track_ids,
        # Filled by _expand_visit (kind + split) then _attach_*.
        "kind": "visit",
        "visit_duration_s": (ended_at - started_at).total_seconds(),
        "zones": [],
        "recording": None,
    }


_COVER_SQL = f"""
    SELECT id, started_at, ended_at, duration_s
    FROM recordings
    WHERE camera_id = $1
      AND started_at <= $2
      AND $2 < {covers_until_sql()}
    ORDER BY started_at DESC LIMIT 1
"""  # noqa: S608


async def _attach_recordings(pool, sightings: list[dict[str, Any]]) -> None:
    """Attach playback info for each sighting.

    `start_at`/`end_at` is the absolute window the player cuts a clip from (it
    spans whatever segments the visit covers). `id`/`seek_seconds` deep-link the
    Recordings page to the segment containing the visit's START — not its
    midpoint: a long visit spans several 60s segments, and anchoring on the
    midpoint segment makes the seek clamp to 0 and drops the entire walk-in.
    Falls back to the midpoint segment only if the start segment was already
    rotated out by retention.
    """
    for s in sightings:
        cam_id = UUID(s["camera"]["id"])
        started = datetime.fromisoformat(s["started_at"])
        ended = datetime.fromisoformat(s["ended_at"])
        row = await pool.fetchrow(_COVER_SQL, cam_id, started)
        if row is None:  # start segment gone (retention) → midpoint fallback
            mid = started + (ended - started) / 2
            row = await pool.fetchrow(_COVER_SQL, cam_id, mid)
        if row is None:
            continue
        # Land a second before the sighting started (Recordings deep-link seek).
        seek = max(0.0, (started - row["started_at"]).total_seconds() - 1.0)
        s["recording"] = {
            "id": str(row["id"]),
            "seek_seconds": seek,
            "start_at": s["started_at"],
            "end_at": s["ended_at"],
        }


def _zone_dwell(
    events: list[tuple[datetime, str]],
) -> tuple[datetime | None, datetime | None, float]:
    """(first enter, last exit, seconds inside) over one zone's time-ordered
    enter/exit stream. Dwell is the SUM of paired enter→exit intervals — NOT
    last_exit minus first_enter, which overcounts wildly when a zone is
    entered early and re-touched late: a car enters through Side Gate at
    04:23, parks in the Carport, and clips Side Gate again on the way out at
    09:11 — the span is 259 min, the actual time inside is ~30."""
    first_enter: datetime | None = None
    last_exit: datetime | None = None
    inside_since: datetime | None = None
    dwell_s = 0.0
    for at, kind in events:
        if kind == "zone_enter":
            if first_enter is None:
                first_enter = at
            if inside_since is None:  # ignore re-enter while already inside
                inside_since = at
        else:  # zone_exit
            last_exit = at
            if inside_since is not None:
                dwell_s += (at - inside_since).total_seconds()
                inside_since = None
    return first_enter, last_exit, dwell_s


def _zone_events_by_sighting(
    rows: list[Any], track_to_sighting: dict[str, dict[str, Any]]
) -> dict[tuple[str, str], dict[str, Any]]:
    """The raw enter/exit stream per (sighting, zone), in time order (the
    query is ORDER BY at)."""
    events_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        s = track_to_sighting.get(str(r["track_id"]))
        if s is None:
            continue
        payload = r["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        zone_id = payload.get("zone_id")
        if not zone_id:
            continue
        e = events_by_key.setdefault(
            (s["id"], zone_id),
            {
                "zone_id": zone_id,
                "zone_name": payload.get("zone_name"),
                "zone_kind": payload.get("zone_kind"),
                "events": [],
            },
        )
        if r["kind"] in ("zone_enter", "zone_exit"):
            e["events"].append((r["at"], r["kind"]))
    return events_by_key


async def _attach_zones(pool, sightings: list[dict[str, Any]]) -> None:
    """Aggregate zone visits per sighting from the events table.

    A sighting's zones list is the set of zones any of its component
    tracks entered, with first/last timestamps for each."""
    # Pool track_ids across all sightings, do one batch query, then
    # fan out. Saves N round-trips on a busy page.
    track_to_sighting = {tid: s for s in sightings for tid in s["track_ids"]}
    if not track_to_sighting:
        return
    rows = await pool.fetch(
        """
        SELECT track_id, kind, at, payload
        FROM events
        WHERE track_id = ANY($1::uuid[])
          AND kind IN ('zone_enter', 'zone_exit', 'zone_dwell')
        ORDER BY at
        """,
        list(track_to_sighting),
    )
    sighting_by_id = {s["id"]: s for s in sightings}
    for (sid, _zid), e in _zone_events_by_sighting(rows, track_to_sighting).items():
        s = sighting_by_id.get(sid)
        if s is None:
            continue
        first_enter, last_exit, dwell_s = _zone_dwell(e["events"])
        s["zones"].append({
            "zone_id": e["zone_id"],
            "zone_name": e["zone_name"],
            "zone_kind": e["zone_kind"],
            "first_enter": first_enter.isoformat() if first_enter else None,
            "last_exit": last_exit.isoformat() if last_exit else None,
            "dwell_s": dwell_s,
        })
    for s in sightings:
        s["zones"].sort(key=lambda z: z.get("first_enter") or "")
