"""The face-anchored body chain — one definition, three callers.

An anonymous person track may inherit a name from a track that a FACE already
confirmed, when the two bodies match closely enough and recently enough. That
rule runs in three places (finalize, the retroactive resweep, and live identity
while the track is still on screen) and used to exist as three hand-copied
SQL blocks. They drifted, and the drift is not theoretical: the cross-camera
envelope carried a five-minute window that never fired.

Measured 2026-07-29 over seven days of this system's own tracks, taking every
face-verified track as ground truth and asking what the body rule alone would
have decided:

    window   threshold  margin    correct   wrong
    5 min    0.40       -              1       -     <- what was deployed
    30 min   0.35       0.05          43       1
    2 h      0.35       0.05          75       2
    6 h      0.35       0.05          90       3
    24 h     0.35       0.05          80       3

The five-minute cross-camera window was the whole gap. The nearest eligible
anchor for an anonymous track sits a median of ~34 h away, so a five-minute
envelope was asking for a coincidence: it named one track in a week while 452
of 585 anonymous tracks had an anchor within 0.40 somewhere. Widening to the
six hours the same-camera branch already used is what recovers them.

Two facts fix the shape of the rule:

* 24 h is WORSE than 6 h (80 correct vs 90). Past a day the nearest body is a
  different day's outfit, so the winner flips to whoever happened to wear
  something similar. Clothing is what this signal actually compares, and it
  expires. One window, six hours, for both branches — the cross-camera window
  is not a separate knob any more, because there was never a reason for it to
  be shorter than the same-camera one, only an assumption that a crossing
  happens within minutes.

* The threshold does NOT transfer across cameras. Same camera holds at 0.32;
  cross-camera needs 0.35 to see anything, and 0.40 is too far — errors jump
  from 3 to 8 between them. A different camera means different lighting and
  pose on the same jacket, which spreads the distance for a true match and for
  a false one alike.

The margin is new, and it is the part that makes widening safe. Distance alone
cannot tell "this is Marko" from "this is the only person we have references
for": at 0.40 with no margin the rule made 8 mistakes, and requiring the winner
to beat the runner-up identity by 0.05 removed a third of them at a cost of two
true matches. It is the same guard the pet/vehicle reference match already
uses, for the same reason — a claim needs a rival to beat, not just a bar to
clear.

What is deliberately NOT relaxed: the anchor must be `face_verified` on a NAMED
identity. A body chain never anchors on another body chain, so a single bad
link cannot propagate into a cluster. The chain also never sets face_verified
on what it names.
"""

from __future__ import annotations

from typing import Any

# `me` must yield: key (any type, groups the answer), camera_id uuid, at
# timestamptz, embedding vector, class_ids int[]. Everything after it is the
# rule, and the parameter numbering continues from the caller's — hence the
# offsets.
#
# The window is measured from `at` — the moment being identified — and is
# SYMMETRIC: the anchor may be earlier or later. Anchoring it to now() instead
# looks identical at finalize (a track ends about when it is finalized) and is
# wrong for the retroactive pass, which reaches back over days; measured, that
# mistake cost the resweep 90% of its decisions. A live track has not ended, so
# its `at` is now().
#
# The thresholds are cast explicitly because the server has nothing else to
# infer them from: inside a CASE arm and next to a `<`, an untyped parameter
# comes through as text and the query dies with "double precision < text" at
# run time. A PREPARE that names its parameter types does NOT reproduce it —
# only the untyped path the driver actually takes does.
#
# DISTINCT ON collapses each candidate identity to its single nearest anchor
# before the identities are ranked against each other, so an identity with
# fifty tracks does not out-rank one with two by sheer count, and `same_cam`
# still describes the anchor that actually won.
_RULE_SQL = """
, _cand AS (
    SELECT DISTINCT ON (m.key, il.global_id)
           m.key,
           il.global_id,
           il.name,
           (t.embedding <=> m.embedding)  AS dist,
           (t.camera_id = m.camera_id)    AS same_cam
    FROM me m
    JOIN tracks t
      ON t.face_verified = true
     -- And verified off a face big enough to have meant something. An anchor
     -- is a NAME that then travels by body, so a name minted from a 35-pixel
     -- face would propagate the mistake to every body that resembles it.
     AND t.face_px >= {min_px}
     AND t.embedding IS NOT NULL
     AND t.class_id = ANY(m.class_ids)
     AND abs(extract(epoch FROM (t.ended_at - m.at))) < ${win}::int * 3600.0
     AND (t.embedding <=> m.embedding)
           < CASE WHEN t.camera_id = m.camera_id
                  THEN ${same}::float8 ELSE ${xcam}::float8 END
    JOIN identity_labels il
      ON il.global_id = t.global_id
     AND il.name IS NOT NULL
    WHERE {extra}
    ORDER BY m.key, il.global_id, (t.embedding <=> m.embedding)
),
_ranked AS (
    SELECT _cand.*,
           ROW_NUMBER() OVER (PARTITION BY key ORDER BY dist)  AS rn,
           LEAD(dist)   OVER (PARTITION BY key ORDER BY dist)  AS runner
    FROM _cand
)
SELECT key, global_id, name, dist, same_cam
FROM _ranked
WHERE rn = 1
  AND (runner IS NULL OR runner - dist >= ${margin}::float8)
"""


# The smallest face, in pixels of its shorter side, that may name anybody —
# here rather than in one caller, because all three paths that can attach a name
# have to answer to it: this chain, the live match, and the finalize re-ID.
#
# Measured over 853 face samples on tracks identified by face in the 14 days to
# 31.08, against the identity whose ENROLLED PHOTOGRAPH is nearest: under 40 px
# they agree 79% of the time, 40-50 85%, 50-60 88%, 60-80 96%, over 80 97%. The
# knee is at 60, where the error rate drops four-fold, and 60 is already what
# the reference side demands of a portrait.
FACE_ID_MIN_PX = 60.0


def anchor_sql(me_cte: str, *, first_param: int, extra: str = "true") -> str:
    """Build the chain query around a caller-supplied `me` CTE.

    `me_cte` is the body of `WITH me AS (...)` and decides what is being
    identified — one finalized track, one live track's samples, or every
    still-anonymous track in a batch. It must project key / camera_id / at /
    embedding / class_ids.

    `first_param` is the number of the first parameter this module owns; the
    caller passes `chain_params()` in that order right after its own. `extra`
    adds a caller-side restriction on candidate anchors (finalize excludes the
    track being finalized).
    """
    return f"WITH me AS ({me_cte})" + _RULE_SQL.format(
        win=first_param,
        same=first_param + 1,
        xcam=first_param + 2,
        margin=first_param + 3,
        extra=extra,
        min_px=FACE_ID_MIN_PX,
    )


def chain_params(config: Any) -> tuple[int, float, float, float]:
    """The four values `anchor_sql` expects, in order."""
    return (
        config.reid_face_anchor_window_hours,
        config.reid_face_anchor_body_threshold,
        config.reid_face_anchor_xcam_threshold,
        config.reid_face_anchor_margin,
    )


def chain_enabled(config: Any) -> bool:
    """Whether the chain can name anything at all.

    A zero window disables it outright. A zero threshold disables that branch
    alone (`< 0` never matches), so one of the two must be positive for the
    query to be worth running.
    """
    if config.reid_face_anchor_window_hours <= 0:
        return False
    return (
        config.reid_face_anchor_body_threshold > 0
        or config.reid_face_anchor_xcam_threshold > 0
    )
