"""The face-anchored body chain's one SQL definition, pinned to two incidents.

Both regressions here shipped to production on 2026-07-29 and were found live:
untyped parameters died at runtime as `double precision < text`, and a window
anchored to now() instead of the moment being identified cost the retroactive
pass 90% of its decisions.
"""

from types import SimpleNamespace

from baba_event_manager.face_anchor import anchor_sql, chain_enabled, chain_params

_ME = "SELECT id AS key, camera_id, now() AS at, embedding, ARRAY[0]::int[] AS class_ids FROM t"


def test_parameters_are_numbered_from_first_param():
    sql = anchor_sql(_ME, first_param=3)
    for placeholder in ("$3::int", "$4::float8", "$5::float8", "$6::float8"):
        assert placeholder in sql, placeholder


def test_thresholds_carry_explicit_casts():
    # asyncpg sends untyped parameters; inside a CASE arm next to `<` the
    # server infers text and the query dies at runtime with
    # "double precision < text". The casts are load-bearing.
    sql = anchor_sql(_ME, first_param=1)
    assert sql.count("::float8") >= 3


def test_window_is_anchored_to_the_moment_being_identified():
    # Measured 2026-07-29: anchoring the window to now() looked identical at
    # finalize and silently cost the retroactive resweep 90% of its decisions,
    # because it reaches back over days.
    sql = anchor_sql(_ME, first_param=1)
    assert "t.ended_at - m.at" in sql
    assert "now() - make_interval" not in sql


def test_extra_predicate_is_injected():
    sql = anchor_sql(_ME, first_param=1, extra="t.id <> m.key")
    assert "t.id <> m.key" in sql


def test_chain_enabled_truth_table():
    cfg = lambda w, same, xcam: SimpleNamespace(  # noqa: E731
        reid_face_anchor_window_hours=w,
        reid_face_anchor_body_threshold=same,
        reid_face_anchor_xcam_threshold=xcam,
        reid_face_anchor_margin=0.05,
    )
    assert not chain_enabled(cfg(0, 0.32, 0.35))  # no window, no chain
    assert not chain_enabled(cfg(6, 0.0, 0.0))    # both branches disabled
    assert chain_enabled(cfg(6, 0.32, 0.0))       # same-camera only
    assert chain_enabled(cfg(6, 0.0, 0.35))       # cross-camera only


def test_chain_params_order_matches_placeholders():
    cfg = SimpleNamespace(
        reid_face_anchor_window_hours=6,
        reid_face_anchor_body_threshold=0.32,
        reid_face_anchor_xcam_threshold=0.35,
        reid_face_anchor_margin=0.05,
    )
    assert chain_params(cfg) == (6, 0.32, 0.35, 0.05)


class _Row(dict):
    """asyncpg rows are mappings; the rule only ever indexes them."""


def _cand(gid, dist):
    return _Row(id=None, global_id=gid, dist=dist, source="track")


def test_a_face_match_has_to_beat_the_next_identity_not_just_the_threshold():
    """Ana drove away at 09:12 on 31.08 and a patio face at 09:56 was given
    her name at 0.669 — out of a neighbourhood of 0.663, 0.669, 0.709, 0.723. It
    won by 0.006, and her own enrolled photographs are 0.994 from that face.

    A distance under the threshold says this face resembles a face we have seen.
    It does not say it resembles THAT person more than anyone else, and that is
    the question an identity answers.
    """
    from baba_event_manager.identity_rules import face_match_survives_rivals

    ana, other = "6dd6c912", "77c52480"
    coin_toss = [_cand(ana, 0.663), _cand(ana, 0.669),
                 _cand(other, 0.709), _cand(ana, 0.723)]
    chosen, gap = face_match_survives_rivals(coin_toss)
    assert chosen is None, "0.046 apart is not an identification"
    assert round(gap, 3) == 0.046

    # The honest case: measured average gap to the next identity is 0.304.
    clear = [_cand(ana, 0.31), _cand(other, 0.62)]
    chosen, gap = face_match_survives_rivals(clear)
    assert chosen is not None and chosen["global_id"] == ana

    # Nobody else in the neighbourhood: nothing to be confused with.
    chosen, gap = face_match_survives_rivals([_cand(ana, 0.40)])
    assert chosen is not None and gap is None
    # An unassigned neighbour cannot claim to be a different person.
    chosen, _ = face_match_survives_rivals([_cand(ana, 0.40), _cand(None, 0.41)])
    assert chosen is not None
    assert face_match_survives_rivals([]) == (None, None)


def test_a_face_too_small_names_nobody():
    """The floor is measured, not chosen. Over 853 face samples on tracks
    identified by face in the 14 days to 31.08, checked against the identity
    whose enrolled photograph is nearest: under 40 px they agree 79% of the
    time, 40-50 85%, 50-60 88%, 60-80 96%, over 80 97%. The error rate drops
    four-fold at 60, which is already what the reference side demands of a
    portrait.

    The patio face given Ana's name at 09:56 on 31.08 — while she had driven
    away at 09:12 — was 34.7 px, and SCRFD was 0.73 sure it was a face. It was.
    There was simply not enough of it to tell her from anyone else.
    """
    from baba_event_manager.identity_rules import FACE_ID_MIN_PX

    assert FACE_ID_MIN_PX == 60.0
    assert FACE_ID_MIN_PX > 34.7


def test_the_recording_is_asked_only_when_the_ring_could_not_answer():
    """Faces are embedded off the ring, and the ring is downscaled — patio
    records 3040x1368 and carries 1280. Measured 31.08 on a man standing still
    and looking into the camera for fifteen seconds: the best face the ring
    could offer was 57.9 px, under the floor, and nothing was said. The same
    moment cut from the segment is 124 x 146 px and matches his enrolled
    photographs at 0.242, with the next person 0.614 behind.

    Re-reading a face that already clears the floor buys nothing, so the query
    that picks work is bounded by the floor on both counts: it must have had a
    face, and it must have been too small to use.
    """
    import re

    from baba_embedder import native_faces

    assert native_faces._FACE_ID_MIN_PX == 60.0
    sql = re.sub(r"\s+", " ", native_faces.NativeFaceReader._pending.__doc__ or "")
    assert "could not use it" in sql
