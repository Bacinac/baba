"""The one question every naming path has to answer the same way.

There were three answers to it. Finalize asked whether the nearest identity
beat the runner-up; the live matcher took `ORDER BY dist LIMIT 1` and never
looked at the runner-up at all; the resweep took `rn = 1` and did the same.
Worse, they were chained: when finalize declined precisely because two
identities could not be told apart, control fell through to the live verdict —
produced by the rule that had never asked. The log said the neighbourhood
could not tell them apart, and the row got a green face badge anyway.

So the rule lives here, and the three callers ask it rather than answer it.
"""

from __future__ import annotations

from typing import Any

from baba_event_manager.face_anchor import FACE_ID_MIN_PX

__all__ = [
    "FACE_ID_MIN_PX",
    "FACE_RIVAL_MARGIN",
    "face_match_survives_rivals",
    "survives_rivals",
]

# How far the winning identity must beat the nearest OTHER identity before a
# face match means anything. A distance under the threshold says this face
# resembles a face we have seen; it does not say it resembles THAT person more
# than anyone else, which is the question an identity answers.
#
# Measured over 604 face samples on tracks matched by face in the seven days to
# 31.08: the average gap to the next identity is 0.304, so an honest match is
# nowhere near this line. But 21% of accepted matches sat under 0.05, and 12%
# had another identity strictly CLOSER than the one they were given. The
# distribution has its knee there — widening to 0.10 adds six points and 0.15
# only nine — so under 0.05 is the undecidable cloud and above it the population
# is genuinely separated.
#
# Ana drove away at 09:12 on 31.08 and a patio face at 09:56 was given her
# name at 0.669, out of a neighbourhood of 0.663, 0.669, 0.709 and 0.723. It won
# by 0.006. Her own enrolled photographs are 0.994 from that face.
FACE_RIVAL_MARGIN = 0.05

# The smallest face that may name anybody. A face under it is still stored and
# still supports a within-visit body anchor; it just does not get to say who
# somebody is. The patio face given Ana's name at 09:56 on 31.08, while she
# had driven away at 09:12, was 34.7 px. Re-exported here so a naming path
# needs one import to get the whole rule.


def face_match_survives_rivals(
    rows: list, margin: float = FACE_RIVAL_MARGIN
) -> tuple[Any, float | None]:
    """The chosen candidate, unless a different identity is just as close.

    Returns (chosen, gap to the nearest rival identity). `chosen` is None when
    the neighbourhood cannot tell two identities apart; the track then falls
    through to the body anchor, which names the same person with honest `body`
    provenance instead of a face badge nothing earned.
    """
    if not rows:
        return None, None
    chosen = rows[0]
    gid = chosen["global_id"]
    rivals = [
        float(r["dist"]) for r in rows[1:]
        if r["global_id"] is not None and gid is not None
        and r["global_id"] != gid and r["dist"] is not None
    ]
    if not rivals or chosen["dist"] is None:
        return chosen, None
    gap = min(rivals) - float(chosen["dist"])
    return (chosen if gap >= margin else None), gap



def survives_rivals(
    best_gid: Any,
    best_dist: float | None,
    runner_up_dist: float | None,
    margin: float = FACE_RIVAL_MARGIN,
) -> bool:
    """The same rule where the candidates arrive as two numbers rather than
    rows — the shape the live matcher and the resweep have.

    A runner-up that does not exist is not a rival: one enrolled identity
    cannot be confused with a second one that is not there.
    """
    if best_gid is None or best_dist is None:
        return False
    if runner_up_dist is None:
        return True
    return runner_up_dist - best_dist >= margin
