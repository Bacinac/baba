"""One question, asked by every naming path, answered in one place.

There were three answers. Finalize asked whether the nearest identity beat the
runner-up; the live matcher took `ORDER BY dist LIMIT 1` and never looked at
the runner-up; the resweep took `rn = 1` and did the same. They were chained,
so when finalize declined *because* two identities could not be told apart,
control fell through to the live verdict — from the rule that had never asked.
The log said the neighbourhood could not tell them apart and the row got a
green face badge anyway.
"""

import re
from pathlib import Path

from baba_event_manager.identity_rules import (
    FACE_ID_MIN_PX,
    FACE_RIVAL_MARGIN,
    face_match_survives_rivals,
    survives_rivals,
)

SRC = Path(__file__).resolve().parent.parent / "services/event-manager/src/baba_event_manager"


def _row(gid, dist):
    return {"global_id": gid, "dist": dist}


def test_a_clear_winner_is_named():
    chosen, gap = face_match_survives_rivals([_row("a", 0.30), _row("b", 0.62)])
    assert chosen is not None and gap is not None and gap > FACE_RIVAL_MARGIN


def test_the_patio_face_of_31_08_is_refused():
    """Given Ana's name at 0.669 out of a neighbourhood of 0.663, 0.669,
    0.709 and 0.723, while she had driven away 44 minutes earlier. Her own
    enrolled photographs are 0.994 from that face. The whole neighbourhood is
    inside the undecidable cloud — even the 0.040 to the next identity."""
    chosen, gap = face_match_survives_rivals(
        [_row("ana", 0.669), _row("someone", 0.709)])
    assert chosen is None and gap is not None and gap < FACE_RIVAL_MARGIN


def test_the_same_answer_from_two_numbers():
    """The live matcher and the resweep hold distances, not rows. Same rule."""
    assert survives_rivals("a", 0.30, 0.62)
    assert not survives_rivals("a", 0.669, 0.675)
    # A runner-up that does not exist is not a rival.
    assert survives_rivals("a", 0.30, None)
    assert not survives_rivals(None, 0.30, None)
    # Clear of the line either way. The line itself is not asserted: a gap is
    # a difference of two floats and the last bit of it means nothing.
    assert survives_rivals("a", 0.30, 0.30 + FACE_RIVAL_MARGIN * 1.2)
    assert not survives_rivals("a", 0.30, 0.30 + FACE_RIVAL_MARGIN * 0.8)


def test_the_two_shapes_agree_on_the_same_neighbourhood():
    for best, rival in ((0.10, 0.90), (0.40, 0.42), (0.669, 0.675), (0.2, 0.3)):
        rows_say = face_match_survives_rivals([_row("a", best), _row("b", rival)])[0]
        assert (rows_say is not None) is survives_rivals("a", best, rival)


def test_no_naming_query_takes_the_nearest_and_asks_nothing():
    """The shape of the bug rather than one instance of it.

    Scoped to queries whose candidates are ENROLLED identities — the `named_face`
    evidence both naming paths draw on. The anonymous body-cluster query orders
    by distance and takes one row too, and rightly: it joins an unnamed track to
    another unnamed track and excludes every named identity, so there is no
    identity there to confuse with a second one."""
    for name in ("live_identity.py", "__main__.py"):
        body = (SRC / name).read_text()
        for sql in re.findall(r'"""(.*?)"""', body, re.S):
            if "named_face" not in sql:
                continue
            # Two shapes carry a runner-up honestly: asking for two rows, or
            # carrying the next distance alongside the best one. What is not
            # allowed is arriving at the caller with only a winner.
            limits = [int(n) for n in re.findall(r"LIMIT (\d+)", sql)]
            carries_runner_up = "next_dist" in sql or "LEAD(" in sql
            assert carries_runner_up or (limits and min(limits) >= 2), (
                f"{name}: a naming query hands the caller a winner and no "
                f"runner-up, so the rule cannot be asked")


def test_the_floor_on_who_may_name_anybody_is_the_measured_one():
    assert FACE_ID_MIN_PX == 60.0


def test_the_live_face_query_is_bounded_in_time():
    """Unclaimed samples keep `track_id IS NULL` forever — finalize claims only
    what falls inside the finished track's window, and 252,260 samples going
    back to 06.08 sat outside one. Without a time bound the live matcher saw
    every orphan ever left under a camera and local id and named new tracks by
    a face they did not have."""
    body = (SRC / "live_identity.py").read_text()
    for sql in re.findall(r'"""(.*?)"""', body, re.S):
        if "track_id IS NULL" not in sql:
            continue
        assert "captured_at >" in sql, (
            "a live sample query is unbounded in time, so it can match an "
            "orphan left under this number by an earlier track")


def test_face_verification_is_written_from_the_row_not_from_a_verdict():
    """`face_verified` is the condition on being an anchor for the body chain,
    so it has to be a fact about the row. It was a remembered verdict, and 360
    of 792 tracks carrying it owned no face at all."""
    body = (SRC / "__main__.py").read_text()
    assert "async def _mark_face_verified" in body
    setters = [sql for sql in re.findall(r'"""(.*?)"""', body, re.S)
               if "face_verified = true" in sql]
    assert setters, "nothing sets face_verified any more — the anchor is dead"
    for sql in setters:
        # Either the statement itself looks for the face, or it selects only
        # rows that have one. Both are true of the two sites; neither was true
        # of the pair this replaced.
        assert "face_embedding IS NOT NULL" in sql, (
            "face_verified is stamped by a statement that never looks for a face")
