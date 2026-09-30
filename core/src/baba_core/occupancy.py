"""The place registry: a fill takes the plate that was read on the way in.

Two independent sensors carry everything this module knows. The scene
evaluator says WHEN a place filled or emptied (hysteresis-committed, agreed
across every camera that views the place). The plate reader says WHO drove the
approach and when (`plate_reads.read_at`, the moment the car showed its
plate). An unnamed episode takes the read that immediately precedes its fill,
and that is the whole binding — no track in the middle, no geometry, no
trigger projecting identities around.

The join is by time alone because the data says time alone is enough:
measured over 21 days on this property, no two places ever filled within
three minutes of each other, and every read that named an arrival sat within
±3 minutes of the fill. The ambiguities that do occur are two cameras reading
the same car within seconds — resolved by preferring the gallery-matched read
— never two cars racing for the window.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from baba_core.classes import PERSON_CLASS_ID

log = logging.getLogger(__name__)

__all__ = [
    "DEPARTURE_BOOKEND_S",
    "announce_episodes",
    "claim_place",
    "departure_window_sql",
    "episode_witness_sql",
    "name_departures",
    "open_missing_episodes",
    "reconcile_places",
    "release_place_if_free",
]

_VEHICLE_CLASS_IDS = (2, 3, 5, 7)

# What a view of a place says about it, as ONE definition. Two callers used to
# ask the same question and answer it differently: the release vote filtered on
# `last_label_raw` while the episode repair read `current_state`, so on 31.08
# west committed `empty` for P2 while shed still stood at `present` — shed had
# been dropped from the vote by a single uncertain read, the place released
# against a view that positively saw the car, and the repair then reopened an
# episode on that same standing `present`. Two rows with one arrival time, and
# two departures announced a minute apart, 19:44 and 19:45.
#
# A view abstains when its camera has stopped producing frames, and when it has
# committed `blinded`. It does NOT abstain for being momentarily unsure:
# `unknown` is never committed, so the view goes on saying what it last saw for
# certain, and that is a vote. Holding a place too long is repaired by the
# read-join and by `open_missing_episodes`; a departure announced for a car
# that never moved is not repairable at all.
_VIEW_SAYS = """
    SELECT sr.place AS place, s.current_state AS says,
           s.current_state_since AS since
    FROM scene_regions sr
    JOIN scene_region_status s ON s.region_id = sr.id
    WHERE sr.place IS NOT NULL AND sr.enabled
      AND s.last_label_raw IS DISTINCT FROM 'stale'
      AND s.current_state IN ('present', 'empty')
"""

# A read precedes its fill: the car shows the plate mid-approach, parks, and
# the classifier commits `present` a few evaluator ticks later. The lookahead
# is clock slack, nothing more — a read genuinely AFTER the fill is somebody
# else driving out.
_READ_LOOKBACK = "5 minutes"
_READ_LOOKAHEAD = "1 minute"

# How far a read may sit from the release it belongs to, and how long a closed
# episode keeps waiting for one. The read lands minutes after the car passed —
# the segment covering the approach has to be written and swept first;
# measured on 28.08, the plate that named a departure was decided nine minutes
# after the place had already closed.
_DEPART_SLACK = "3 minutes"
_DEPART_PATIENCE = "1 hour"

# The clip either end of a stay. A car that stood fourteen hours is two moments
# and a number, never a fourteen-hour span to sit through: both windows END on
# the registry's own moment — the one the place filled, the one it emptied —
# and how long it stood rides as `stood_s`.
#
# Sixty seconds because the fill LAGS the car: the evaluator needs three
# agreeing reads to commit, so the place is declared full about half a minute
# after the car stopped. Measured across the arrivals on record, the plate
# showed 31, 31, 32 and 137 seconds before the fill and the gate opened 37 to
# 45 seconds before it; a minute catches the approach for all but the car that
# drove past another place first. Departures are tighter still — the place
# emptied 2 and 21 seconds after the plate went by.
DEPARTURE_BOOKEND_S = 60
_BOOKEND_S = DEPARTURE_BOOKEND_S

# How far a departure reaches back for the walk to the car. The bookend above is
# measured on the CAR, and a departure does not begin with the car: it begins
# when whoever is leaving comes out and crosses the yard. Measured on every
# departure on record that has a person track on the witness camera, the walk
# starts 34, 48, 68 and 98 seconds before the place empties — so a sixty-second
# bookend catches the first of those four and opens the other three on a car
# already reversing.
#
# The clip starts at that person instead, and two minutes is both the cap on how
# far that may reach — past it, somebody who happened to pass rather than
# somebody walking to this car — and what a departure gets when there is NO
# person track to reach for. That second half matters as much as the first: the
# walk only becomes a track if it survives the zone gate, and two of the six
# real departures on record left none on the witness camera at all. Reaching
# blind is the only thing that covers those, and two minutes clears the longest
# walk measured (98 s).
#
# It never comes out SHORTER than the bookend — a person who appears thirty
# seconds before the car moves does not shorten it.
#
# Not more than two minutes, because west is HEVC and its clips are re-encoded
# rather than copied: the encoder is capped at four minutes and a longer window
# arrives in pieces, each with its own progress bar. Measured on this camera,
# 33 s of HEVC costs ~2.9 s to encode, so five minutes would be roughly half a
# minute of encoding per view, on the box that is already at its inference
# ceiling. Two minutes is ~10 s and ~52 MB.
_APPROACH_MAX_S = 120

# How far back an announcement reaches. An announcement is about something
# that has just happened; without this the first pass after a deploy would
# replay every episode in the registry onto the feed and the bus, dated across
# the month they actually happened.
_ANNOUNCE_WINDOW = "1 hour"

# How long an end of a stay waits for a name before it is announced anyway. The
# read lands minutes after the car passed — the segment covering the approach
# has to be written and swept first — so announcing the instant a place fills or
# empties publishes "somebody arrived" for nearly every arrival there is. Past
# this, the name is not coming and the movement is a fact on its own.
#
# Ten minutes because that is what was measured: from the edge of the episode to
# the read being decided ran 3:00 for the departure of 29.08 11:21 and 6:06 for
# the arrival at 12:36 — the latter under a six-minute grace, which it cleared by
# six seconds and could as easily have missed. A grace shorter than the reader's
# own latency does not hold anything back; it just publishes the wrong answer
# first.
_ANNOUNCE_GRACE = "10 minutes"


def vehicle_behind_place_sql(*, first_param: int) -> str:
    """Whether a car was actually there, as one predicate both callers use.

    `ever_active` is the whole point: a standing car re-born under IR never
    drove, so it never sets it, and twenty nights of nameless occupants came
    from exactly that. A phantom has no active vehicle track at all, which is
    why the window can be generous without letting one through.

    Parameters, in order from `first_param`: place, vehicle class ids, the
    moment to look around, and how far either side of it to look.
    """
    p = first_param
    return f"""
        EXISTS (
            SELECT 1 FROM tracks t
             WHERE t.class_id = ANY(${p + 1}::int[])
               AND t.ever_active
               AND COALESCE(t.ended_at, now())
                     > ${p + 2}::timestamptz - ${p + 3}::interval
               AND COALESCE(t.started_at, t.ended_at)
                     < ${p + 2}::timestamptz + ${p + 3}::interval
               AND EXISTS (SELECT 1 FROM scene_regions sr
                            WHERE sr.camera_id = t.camera_id
                              AND sr.place = ${p} AND sr.enabled)
        )
    """  # noqa: S608


# How far either side of the fill a car may have been seen. One window, not
# two: the repair asks the same question as the live claim, of a moment that
# has already passed rather than of now, so the same three minutes bound it.
#
# Widening was tried and measured against this property's own footage. At 3 AM
# on 04.09 a `car` track on west ran 02:52:31 to 03:00:12 with `ever_active`
# set — the IR flap of a parked car, not an arrival — and it satisfies this
# predicate at three minutes, ten and thirty alike. ⚠️ So `ever_active` is not
# by itself the phantom filter its docstring claims; widening only buys more
# ways in without buying a single real arrival.
_CLAIM_WINDOW = timedelta(minutes=3)

# How long a place saying `present` with no episode is still news. It runs
# every thirty seconds and a phantom stays present for hours, so saying so
# once a tick forever would bury the arrivals this is meant to surface.
_FRESH_CONTRADICTION = timedelta(minutes=30)


async def claim_place(conn: Any, place: str) -> None:
    """Open an episode for `place` — unnamed, and only if a car really arrived.

    WHO is decided by `reconcile_places` once the read lands. `ever_active` is
    what keeps the phantoms out: a standing car re-born under IR never drove,
    so it never sets it, and twenty nights of nameless occupants came from
    exactly that.
    """
    arrived = await conn.fetchval(
        f"SELECT {vehicle_behind_place_sql(first_param=1)}",
        place,
        list(_VEHICLE_CLASS_IDS),
        datetime.now(UTC),
        _CLAIM_WINDOW,
    )
    if not arrived:
        log.warning(
            "place %s reported occupied with no vehicle behind it — "
            "no episode opened", place,
        )
        return

    row = await conn.fetchrow(
        """
        INSERT INTO place_occupancy (place, evidence, occupied_since)
        VALUES ($1, 'unknown', now())
        ON CONFLICT (place) WHERE released_at IS NULL DO NOTHING
        RETURNING id
        """,
        place,
    )
    if row is None:
        return  # already occupied — nothing to do
    log.info("place %s claimed, occupant not yet decided", place)


def episode_witness_sql(place: str, plate_camera: str) -> str:
    """Which camera watched this episode, as ONE definition.

    A place is not a camera and several may watch it, so every consumer needs
    the one with the car in it. Two of them answered differently: the
    announcement preferred the camera that READ THE PLATE — it is pointed at the
    approach, which is why it could read it at all — while the feed took
    whichever vehicle track ended nearest the release. Over twenty days they
    disagreed on 6 of 25 closed episodes, and because the departure window is
    computed FROM the witness, the tile an operator plays and the event on the
    bus described moments up to a minute apart. A shared formula over unshared
    inputs is not one definition.

    A scalar sub-select, so a caller drops it in wherever it already has the
    episode and its plate read.
    """
    return f"""(
        SELECT sr.camera_id FROM scene_regions sr
          JOIN cameras c ON c.id = sr.camera_id
         WHERE sr.place = {place} AND sr.enabled
         ORDER BY (sr.camera_id = {plate_camera}) DESC, c.slug
         LIMIT 1)"""  # noqa: S608


def departure_window_sql(released: str, camera: str) -> str:
    """When the clip of a departure should start, as ONE definition.

    Two places answer this question — the announcement that reaches the bus and
    the Activity feed's own departure row — and they answered it differently:
    sixty seconds here, forty there, neither reaching the walk. The operator
    watched the feed, so fixing the announcement changed nothing he could see.

    `released` and `camera` are SQL expressions for the moment the place emptied
    and the camera that watched it, so a caller can hand over whatever it has
    already computed.
    """
    bookend = f"{released} - interval '{_BOOKEND_S} seconds'"
    cap = f"{released} - interval '{_APPROACH_MAX_S} seconds'"
    return f"""LEAST({bookend}, GREATEST({cap},
                COALESCE((SELECT min(t.started_at) FROM tracks t
                           WHERE t.camera_id = {camera}
                             AND t.class_id = {PERSON_CLASS_ID}
                             AND t.ended_at BETWEEN {cap} AND {released}),
                         {cap})))"""  # noqa: S608


class _NothingNamed(Exception):
    """Raised inside the per-episode transaction to roll a demote back when
    the naming it was clearing the way for lands no row."""


async def open_missing_episodes(conn: Any) -> None:
    """Give every occupied place an episode, whatever happened at the fill.

    `claim_place` runs on the empty->present transition and nowhere else, so a
    place whose fill was refused (no vehicle track had ended within three
    minutes) — or whose fill landed while this service was down or
    redeploying — stays `present` with no episode, and nothing ever looks
    again. Measured: P1 stood exactly like that for nineteen hours, while the
    footage of its arrival held a perfectly legible plate.

    The registry describes the places, so `present` with no open episode is a
    contradiction to be repaired rather than a state to be tolerated. The
    episode is backdated to the earliest moment any view committed to
    `present` — the car arrived then, whatever failed to be written at the
    time — and opens unnamed, which the read-join then fills in.

    It asks whether a car was there, which it did not. `claim_place` refuses a
    fill with no vehicle behind it and writes that down; this then opened an
    episode for the same place thirty seconds later, on no condition but the
    views, and every refusal was undone. A gate reversed on the next tick is
    not a gate. Same predicate now, same window, asked of the moment being
    repaired rather than of now.

    The backdate stops at the last release of that place. A view that never
    left `present` carries a `current_state_since` from before a departure that
    has already been announced, and reaching back past it writes a second
    episode over the first one's lifetime: P1 and P2 each carry a pair sharing
    one arrival second, 28.08 22:28:14 and 31.08 17:31:19.
    """
    candidates = await conn.fetch(
        f"""
        SELECT v.place,
               GREATEST(min(v.since),
                        COALESCE((SELECT max(o.released_at)
                                    FROM place_occupancy o
                                   WHERE o.place = v.place),
                                 min(v.since))) AS since
        FROM ({_VIEW_SAYS}) v
        WHERE v.says = 'present'
          AND NOT EXISTS (SELECT 1 FROM place_occupancy o2
                           WHERE o2.place = v.place AND o2.released_at IS NULL)
        GROUP BY v.place
        """  # noqa: S608
    )
    for cand in candidates:
        # The same question the live claim asks, asked of the moment being
        # repaired rather than of now. Without it this undid every refusal
        # `claim_place` made: it opened an episode for any place whose views
        # said `present`, with no condition about a vehicle at all, thirty
        # seconds after the refusal was written down. A gate that is reversed
        # on the next tick is not a gate.
        arrived = await conn.fetchval(
            f"SELECT {vehicle_behind_place_sql(first_param=1)}",
            cand["place"], list(_VEHICLE_CLASS_IDS), cand["since"], _CLAIM_WINDOW,
        )
        if not arrived:
            # Only while the contradiction is fresh. A phantom stays present
            # for hours and this runs every thirty seconds; saying so once a
            # tick forever would bury the arrivals it is meant to surface.
            if datetime.now(UTC) - cand["since"] < _FRESH_CONTRADICTION:
                log.warning(
                    "place %s says occupied with no episode and no vehicle "
                    "behind it around %s — leaving it unopened",
                    cand["place"], cand["since"],
                )
            continue
        row = await conn.fetchrow(
            """
            INSERT INTO place_occupancy (place, evidence, occupied_since)
            VALUES ($1, 'unknown', $2)
            ON CONFLICT (place) WHERE released_at IS NULL DO NOTHING
            RETURNING place, occupied_since
            """,
            cand["place"], cand["since"],
        )
        if row is not None:
            log.warning(
                "place %s was occupied with no episode — opened one, "
                "backdated to %s", row["place"], row["occupied_since"],
            )


async def demote_rival_places(
    conn: Any, global_id: Any, *, keeping: Any
) -> list[Any]:
    """Take this identity off every other place it is still standing in.

    One car is in one bay. A place proven to hold it now settles the question
    for every place that only believed it did — a stuck view, a name the
    registry put down before there was a better answer.

    The read STAYS on the demoted row. Freeing it would hand it back to the
    reconcile query, the stale episode would re-take it and demote the fresh
    one, and the name would ping-pong between the two places on every pass —
    measured on the dryrun replay before this said so. `evidence='unknown'` is
    what tells the readers not to render that read's plate.

    Must be called inside the same transaction as the naming it follows from:
    a demote justified by a naming that then fails has stripped a correct
    episode for nothing.

    Returns the places given up, so the caller can say whose they were.
    """
    return await conn.fetch(
        """
        UPDATE place_occupancy
           SET global_id = NULL, evidence = 'unknown'
         WHERE released_at IS NULL AND global_id = $1
           AND id <> $2
        RETURNING place
        """,
        global_id, keeping,
    )


async def reconcile_places(conn: Any) -> None:
    """Give every unnamed open episode the read that walked in ahead of it.

    Runs on its own interval and again after each successful read, because the
    two facts arrive minutes apart: the place fills seconds after the car
    stops, the read lands only once the segment covering the approach has been
    written and swept.

    A read is consumed at most once (unique index backs the check here), and
    among several candidates the NEWEST wins, gallery-matched first — the
    measured ambiguity is one car read by two cameras, one of them garbling
    the string, and the garbled read matches nothing. An episode that takes a
    matched identity first evicts that identity's stale open episode
    elsewhere: a car proven to have just arrived here cannot still be
    standing where a stuck view left it.
    """
    rows = await conn.fetch(
        f"""
        SELECT o.id, o.place, r.id AS read_id, r.global_id, r.plate_text
        FROM place_occupancy o
        JOIN LATERAL (
            SELECT r.id, r.global_id, r.plate_text
            FROM plate_reads r
            WHERE r.read_at BETWEEN o.occupied_since - interval '{_READ_LOOKBACK}'
                                AND o.occupied_since + interval '{_READ_LOOKAHEAD}'
              AND NOT EXISTS (SELECT 1 FROM place_occupancy o2
                               WHERE o2.plate_read_id = r.id)
              -- A read taken while a car with the SAME identity or the SAME
              -- registration was releasing a place is that car driving OUT.
              -- West reads departures too (measured: about half of them),
              -- and without this a household car leaving handed its name to
              -- whoever filled the spot minutes later — the join alone
              -- cannot tell inbound from outbound.
              AND NOT EXISTS (
                  SELECT 1 FROM place_occupancy done
                  LEFT JOIN plate_reads dr ON dr.id = done.plate_read_id
                  WHERE done.released_at BETWEEN r.read_at - interval '3 minutes'
                                             AND r.read_at + interval '3 minutes'
                    AND ((r.global_id IS NOT NULL
                          AND done.global_id = r.global_id)
                         OR dr.plate_text = r.plate_text))
            ORDER BY (r.global_id IS NOT NULL) DESC, r.read_at DESC,
                     r.readings DESC
            LIMIT 1
        ) r ON true
        WHERE o.released_at IS NULL
          AND o.global_id IS NULL
          -- A demoted episode keeps its read and stays out of here: the car
          -- it named was proven elsewhere, and no read still in the window
          -- can say anything new about a fill this old.
          AND o.plate_read_id IS NULL
        -- Oldest fill first: when one read sits in two fills' windows, it
        -- belongs to the fill that happened first — live, that order is
        -- enforced by time itself (the first fill reconciles minutes before
        -- the second exists); after a restart it has to be said.
        ORDER BY o.occupied_since
        """  # noqa: S608
    )
    for row in rows:
        demoted: list[Any] = []
        named = None
        try:
            async with conn.transaction():
                if row["global_id"] is not None:
                    demoted = await demote_rival_places(
                        conn, row["global_id"], keeping=row["id"],
                    )
                named = await conn.fetchrow(
                    """
                    UPDATE place_occupancy
                       SET global_id = $2, plate_read_id = $3, evidence = 'plate'
                     WHERE id = $1 AND released_at IS NULL
                       AND global_id IS NULL AND plate_read_id IS NULL
                       AND NOT EXISTS (SELECT 1 FROM place_occupancy o2
                                        WHERE o2.plate_read_id = $3)
                    RETURNING id
                    """,
                    row["id"], row["global_id"], row["read_id"],
                )
                if named is None:
                    # The read was consumed between the SELECT and here (two
                    # fills can share one candidate in a single pass). The
                    # demote above was justified only by a naming that did
                    # not happen — roll the whole step back rather than strip
                    # a correct episode for nothing.
                    raise _NothingNamed
        except _NothingNamed:
            continue
        except Exception:
            # One episode's step failing (a concurrent reconciler winning a
            # unique-index race, a transient error) must not abort the rest
            # of the pass; the next sweep retries what was rolled back.
            log.exception("reconcile step failed for place %s — continuing",
                          row["place"])
            continue
        for d in demoted:
            log.warning(
                "place %s gives up %s — the car was just read arriving "
                "at %s, so whatever stands here is somebody else",
                d["place"], row["global_id"], row["place"],
            )
        log.warning(
            "place %s taken by %s (plate %r)",
            row["place"], row["global_id"] or "an unenrolled visitor",
            row["plate_text"],
        )


async def name_departures(conn: Any) -> None:
    """Give a closed, still-unnamed episode the read taken as the car drove out.

    Half of all closures are witnessed: west reads the approach in both
    directions, so the car that leaves shows its plate on the way past. The
    arrival join deliberately refuses those reads — a read after a fill is
    somebody driving out, and handing it to the next arrival is how one
    household car named another's spot. But refusing them left them unused,
    and an episode nobody could name on the way in is exactly the one whose
    departure read would say who it was: on 28.08 `SI421KL` was read at
    08:45:02 with a perfect score, the place closed two seconds later as an
    unidentified vehicle, and the read was decided nine minutes after that and
    then sat there.

    An arrival still outranks a departure for the same read. A read inside any
    open unnamed episode's arrival window is left alone, so this can only take
    what the join has already declined.
    """
    rows = await conn.fetch(
        f"""
        SELECT o.id, o.place, r.id AS read_id, r.global_id, r.plate_text
        FROM place_occupancy o
        JOIN LATERAL (
            SELECT r.id, r.global_id, r.plate_text
            FROM plate_reads r
            WHERE r.read_at BETWEEN o.released_at - interval '{_DEPART_SLACK}'
                                AND o.released_at + interval '{_DEPART_SLACK}'
              AND NOT EXISTS (SELECT 1 FROM place_occupancy o2
                               WHERE o2.plate_read_id = r.id)
              AND NOT EXISTS (
                  SELECT 1 FROM place_occupancy op
                   WHERE op.released_at IS NULL
                     AND op.global_id IS NULL
                     AND op.plate_read_id IS NULL
                     AND r.read_at BETWEEN op.occupied_since - interval '{_READ_LOOKBACK}'
                                       AND op.occupied_since + interval '{_READ_LOOKAHEAD}')
            ORDER BY (r.global_id IS NOT NULL) DESC,
                     abs(extract(epoch FROM r.read_at - o.released_at)),
                     r.readings DESC
            LIMIT 1
        ) r ON true
        WHERE o.released_at > now() - interval '{_DEPART_PATIENCE}'
          AND o.global_id IS NULL
          AND o.plate_read_id IS NULL
        ORDER BY o.released_at
        """  # noqa: S608
    )
    for row in rows:
        try:
            named = await conn.fetchrow(
                """
                UPDATE place_occupancy
                   SET global_id = $2, plate_read_id = $3, evidence = 'plate'
                 WHERE id = $1 AND global_id IS NULL AND plate_read_id IS NULL
                   AND NOT EXISTS (SELECT 1 FROM place_occupancy o2
                                    WHERE o2.plate_read_id = $3)
                RETURNING id
                """,
                row["id"], row["global_id"], row["read_id"],
            )
        except Exception:
            log.exception("departure naming failed for place %s — continuing",
                          row["place"])
            continue
        if named is None:
            continue
        log.warning(
            "place %s was left by %s (plate %r) — named from the departure read",
            row["place"], row["global_id"] or "an unenrolled visitor",
            row["plate_text"],
        )


async def announce_episodes(conn: Any) -> None:
    """Say who arrived and who left, once each, from the registry.

    The bus carries `parked` — who is standing there NOW — and a state can only
    ever describe the present. Nothing said a car HAD arrived or HAD left, so a
    consumer had to watch the state change and remember it, and a name learned
    after the place emptied (a plate read on the way out, decided minutes later)
    had nowhere to go at all.

    An episode is the record of both ends: it opened when the car arrived and
    closed when it left. So both are announced from it, and the departure is
    announced only after `name_departures` has had its pass — which is what
    lets a stay named at either end reach a consumer with its name attached.

    Announced at most once, and the events table is what says so: a flag column
    would be a second place to be wrong about the same fact.
    """
    for kind, at_col in (
        ("vehicle_arrived", "occupied_since"),
        ("vehicle_left", "released_at"),
    ):
        # Waiting is for the answer, not for the clock: an episode that already
        # HAS a name has nothing left to wait for and goes out at once — which
        # is faster than the old fixed grace, not slower. Only a nameless one
        # sits out the grace, and then says so honestly.
        #
        # The departure had no wait at all, and that is exactly what it needed
        # most: on 29.08 P2 was announced unnamed at 11:21:36 and the read that
        # named it was decided at 11:24:36. The name reached the registry and
        # nobody downstream ever heard it.
        named = "(o.global_id IS NOT NULL OR o.plate_read_id IS NOT NULL)"
        edge = "occupied_since" if kind == "vehicle_arrived" else "released_at"
        ready = (
            f"o.{edge} IS NOT NULL AND ({named} OR "
            f"o.{edge} < now() - interval '{_ANNOUNCE_GRACE}')"
        )
        fresh = f"o.{at_col} > now() - interval '{_ANNOUNCE_WINDOW}'"
        # How long it stood belongs to the departure. An arrival announced late
        # — for a car that has already left again — would otherwise carry a
        # duration nobody could have known at the moment it is reporting.
        stood = (
            "extract(epoch FROM o.released_at - o.occupied_since)::bigint"
            if kind == "vehicle_left"
            else "NULL"
        )
        # An arrival is the car's: it drives in, and whoever was in it gets out
        # afterwards. A departure is the walk's.
        starts_at = (
            departure_window_sql("o.released_at", "sr.camera_id")
            if kind == "vehicle_left"
            else f"o.{at_col} - interval '{_BOOKEND_S} seconds'"
        )
        rows = await conn.fetch(
            f"""
            INSERT INTO events (camera_id, kind, at, payload)
            SELECT sr.camera_id, '{kind}', o.{at_col},
                   jsonb_build_object(
                       'episode_id', o.id,
                       'place', o.place,
                       'global_id', o.global_id,
                       'name', COALESCE(il.name, CASE WHEN o.evidence = 'plate'
                                                      THEN pr.plate_text END, ''),
                       'evidence', o.evidence,
                       'stood_s', {stood},
                       'started_at', {starts_at},
                       'ended_at', o.{at_col})
            FROM place_occupancy o
            LEFT JOIN identity_labels il ON il.global_id = o.global_id
            LEFT JOIN plate_reads pr ON pr.id = o.plate_read_id
            -- The witness: `place_occupancy` names a place, a place is not a
            -- camera, and a row nothing can play is worse than no row. The
            -- camera that READ THE PLATE is the one that watched the car come
            -- or go — it is pointed at the approach, which is why it could read
            -- it at all. Alphabetical order picked Shed for Ana's arrival on
            -- 28.08 while West held the drive-in and the plate; a clip is worth
            -- having only if it shows the thing it claims to.
            JOIN LATERAL (
                SELECT {episode_witness_sql("o.place", "pr.camera_id")} AS camera_id
            ) sr ON true
            WHERE {ready} AND {fresh}
              AND NOT EXISTS (SELECT 1 FROM events e
                               WHERE e.kind = '{kind}'
                                 AND e.payload->>'episode_id' = o.id::text)
            RETURNING (payload->>'place') AS place, (payload->>'name') AS name
            """  # noqa: S608
        )
        for r in rows:
            log.info("%s: %s at %s", kind, r["name"] or "an unidentified vehicle",
                     r["place"])


async def release_place_if_free(conn: Any, place: str) -> None:
    """Close the open episode for `place` — but only on a positive `empty`.

    Touches nothing but this table: the occupant is already on the row, so
    nothing pruned or renamed elsewhere can wedge a place open.

    A release needs somebody to positively read `empty` and nobody to be
    reading `present`; a place every view has lost sight of is held. Who is
    entitled to a vote is `_VIEW_SAYS`, which the episode repair reads too.
    """
    views = await conn.fetchrow(
        f"""
        SELECT bool_or(v.says = 'present') AS any_present,
               bool_or(v.says = 'empty')   AS any_empty
        FROM ({_VIEW_SAYS}) v
        WHERE v.place = $1
        """,  # noqa: S608
        place,
    )
    if views is None or views["any_present"] or not views["any_empty"]:
        return
    closed = await conn.fetchrow(
        """
        UPDATE place_occupancy
           SET released_at = now()
         WHERE place = $1 AND released_at IS NULL
        RETURNING global_id,
                  extract(epoch FROM (now() - occupied_since))::bigint AS held
        """,
        place,
    )
    if closed is None:
        return
    log.info(
        "place %s released after %d s by %s",
        place, closed["held"], closed["global_id"] or "an unidentified vehicle",
    )
