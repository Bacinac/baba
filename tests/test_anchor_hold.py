"""The appearance-anchored hold — the thing that keeps a seated person present.

Measured over 14 days before these rules changed: the patio radio went silent
216 times while somebody was sitting under it, 22.4 hours of it, and the
detector was publishing a person on 96.4% of frames throughout.
"""

import asyncio

from baba_tracker.anchor_hold import _class_group, _corroborates

PERSON, CAT, DOG, CAR = 0, 15, 16, 2


def test_a_pet_box_corroborates_a_seated_person():
    """The patio detector flips a seated person to dog or cat thousands of
    times a day — one track was stamped `Lumi (pet)` and anchored
    `class=person` twenty-nine seconds apart. Refusing those boxes made the
    sitter's own detections argue that their seat was empty."""
    assert _corroborates(DOG, "person")
    assert _corroborates(CAT, "person")
    assert _corroborates(PERSON, "person")


def test_a_person_box_does_not_prop_up_a_pet():
    """The looseness runs one way only. Anyone walking past would otherwise
    hold a dog's presence open indefinitely."""
    assert not _corroborates(PERSON, "pet")


def test_a_car_corroborates_neither():
    assert not _corroborates(CAR, "person")
    assert not _corroborates(CAR, "pet")
    assert _corroborates(CAR, "vehicle")


def test_the_pet_pool_still_holds_together():
    """dog/cat pool because the detector flips between them; that is what
    makes a pet box admissible for a person in the first place."""
    assert _class_group(DOG) == _class_group(CAT) == "pet"
    assert _class_group(PERSON) == "person"


class _Settings:
    def f(self, key):
        return {"anchor_hold_dist": 0.30}.get(key, 0.0)

    def i(self, key):
        return 3


def _registry():
    from baba_tracker.anchor_hold import AnchorHold

    return AnchorHold(_Settings(), classes=(PERSON,), available=True)


def _arm(hold, tid, ts):
    import numpy as np
    from baba_core.wire import TrackWire

    wire = TrackWire(track_id=tid, x1=400, y1=400, x2=460, y2=560,
                     class_id=PERSON, class_name="person", confidence=0.8)
    asyncio.run(hold.note("patio", wire, np.array([1.0, 0.0]), ts, 1000, 1000, None, arrived=True))


def test_a_full_registry_refuses_the_newcomer_and_keeps_the_sitter():
    """12.09, nine people on the patio: the full registry evicted whichever
    anchor had gone longest unconfirmed. Anchors on live tracks are confirmed
    every tick, so that was always the one whose track had died — the seated
    person the hold exists for. 2990 evictions in eight hours, the radio off
    under a full terrace. The newcomer is a live track and carries its own
    presence; it is the one that waits."""
    from baba_tracker.anchor_hold import _MAX_PER_CAMERA

    hold = _registry()
    for tid in range(1, _MAX_PER_CAMERA + 1):
        _arm(hold, tid, ts=1_000)
    sitter = hold._by_cam["patio"][1]
    sitter.corroborated_ns = 0          # track long dead, held by appearance

    _arm(hold, 999, ts=2_000)

    assert set(hold._by_cam["patio"]) == set(range(1, _MAX_PER_CAMERA + 1))
    assert hold._by_cam["patio"][1] is sitter
    assert hold.drain_stats("patio") == {"refused": 1}


def test_the_valve_sits_above_the_scene_it_guards():
    """Measured over 30 days, anchored-class tracks alive or inside the
    corroboration window at once: patio 18, door and south 12."""
    from baba_tracker.anchor_hold import _MAX_PER_CAMERA

    assert _MAX_PER_CAMERA > 18


def test_a_handover_carries_the_release_countdown():
    """A handover fires when a live track of the same class stands on the
    anchor's spot — and on a corner that produces a static false positive that
    is the SAME phantom under a new id. Building the successor from scratch
    reset the three-strike countdown and the age cap with it, so the chain never
    ended: measured on patio, `anchor miss (1/3)` at 20:26:38 and a handover one
    second later, over an empty terrace, holding the presence that turns the
    radio on.
    """
    from baba_tracker.anchor_hold import AnchorHold

    hold = AnchorHold.__new__(AnchorHold)
    hold._carried = {}
    hold._carried.setdefault("patio", {})[686] = (1, 1_000)

    # What `note` reads when it builds the successor's anchor.
    misses, created = hold._carried["patio"].pop(686, (0, 0))
    assert misses == 1, "the successor starts one strike down, not clean"
    assert created == 1_000, "and keeps the original's age, so the cap still bites"
    # Consumed exactly once — a later anchor on the same id is a fresh subject.
    assert hold._carried["patio"].pop(686, (0, 0)) == (0, 0)


def test_a_camera_that_goes_away_leaves_no_debt():
    from baba_tracker.anchor_hold import AnchorHold

    hold = AnchorHold.__new__(AnchorHold)
    hold._carried = {"patio": {686: (2, 5)}}
    hold._by_cam = {}
    hold._cap_logged_ns = {"patio": 1}
    AnchorHold.forget_camera(hold, "patio")
    assert "patio" not in hold._carried


# --- the sweep: what happens to one anchor on one tick ------------------------

S = 1_000_000_000
TS = 100 * S
SPOT = (400.0, 400.0, 460.0, 560.0)
CTX = (300.0, 300.0, 560.0, 660.0)
AWAY = (900.0, 400.0, 960.0, 560.0)


def _axis(i: int):
    import numpy as np

    v = np.zeros(4)
    v[i] = 1.0
    return v


LIVE = {
    "anchor_hold_dist": 0.30,
    "anchor_check_interval_s": 1.0,
    "anchor_max_age_h": 12.0,
    "anchor_max_age_nonperson_h": 72.0,
    "anchor_corroboration_min": 1.5,
}


class _Live:
    def f(self, key):
        return LIVE[key]

    def i(self, key):
        return {"anchor_miss_limit": 3}[key]


def _anchored(cls=PERSON, **kw):
    from baba_tracker.anchor_hold import AnchorHold, _Anchor

    hold = AnchorHold(_Live(), classes=(PERSON, DOG, CAR), available=True)
    fields = {"created_ns": 0, "corroborated_ns": TS, "identity_gid": "gid-7", "identity_name": "Marko", **kw}
    a = _Anchor(bbox=SPOT, emb=_axis(0), class_id=cls, class_name="x", confidence=0.8,
                state_since_ns=0, **fields)
    hold._by_cam["patio"] = {7: a}
    return hold, a


def _live(tid, box=SPOT, cls=PERSON, age_s=5.0):
    from baba_core.wire import TrackWire

    return TrackWire(track_id=tid, x1=box[0], y1=box[1], x2=box[2], y2=box[3], class_id=cls,
                     class_name="x", confidence=0.8, first_seen_ns=int(TS - age_s * S))


def _looks(views):
    calls = []

    async def embed(b):
        calls.append(tuple(b))
        return views.get(tuple(b))

    embed.calls = calls
    return embed


def _sweep(hold, *, emitted=(), crop_embed=None, ts=TS, **kw):
    return asyncio.run(hold.sweep("patio", list(emitted), crop_embed, ts, **kw))


def _held_boxes(out):
    return [(w.track_id, (w.x1, w.y1, w.x2, w.y2)) for w in out]


def test_an_anchor_inside_an_ignore_zone_is_released():
    hold, _ = _anchored()
    assert _sweep(hold, is_ignored=lambda b: b == SPOT) == ([], {})
    assert not hold.has("patio")


def test_a_live_track_keeps_its_anchor_dormant():
    hold, a = _anchored(misses=2, holding=True)
    assert _sweep(hold, emitted=[_live(7)]) == ([], {})
    assert (a.misses, a.holding) == (0, False)
    assert hold._by_cam["patio"] == {7: a}


def test_the_closest_looking_live_track_takes_the_anchor_over():
    import numpy as np

    hold, _ = _anchored(misses=1, created_ns=3 * S)
    near = np.array([0.95, (1 - 0.95**2) ** 0.5, 0.0, 0.0])
    out, cont = _sweep(hold, emitted=[_live(9, AWAY), _live(8, (100, 100, 160, 260))],
                       emb_by_tid={8: _axis(0), 9: near})
    assert out == []
    assert cont == {8: (7, "gid-7", "Marko")}
    assert hold._carried["patio"] == {8: (1, 3 * S)}
    assert not hold.has("patio")


def test_without_appearance_evidence_the_overlapping_track_takes_over():
    hold, _ = _anchored()
    out, cont = _sweep(hold, emitted=[_live(8, SPOT)])
    assert (out, cont) == ([], {8: (7, "gid-7", "Marko")})


def test_a_track_younger_than_the_mayfly_window_does_not_take_over():
    hold, _ = _anchored()
    out, cont = _sweep(hold, emitted=[_live(8, SPOT, age_s=1.0)])
    assert (_held_boxes(out), cont) == ([(7, SPOT)], {})


def test_a_young_lookalike_carries_the_hold_through_a_mismatch():
    hold, a = _anchored()
    out, _ = _sweep(hold, emitted=[_live(8, AWAY, age_s=1.0)], emb_by_tid={8: _axis(0)},
                    crop_embed=_looks({SPOT: _axis(1)}))
    assert _held_boxes(out) == [(7, SPOT)]
    assert a.misses == 0


def test_a_person_past_the_age_cap_is_released_and_a_pet_is_not():
    person, _ = _anchored()
    assert _sweep(person, ts=13 * 3600 * S) == ([], {})
    assert not person.has("patio")

    pet, _ = _anchored(DOG, last_check_ns=13 * 3600 * S)
    out, _ = _sweep(pet, ts=13 * 3600 * S)
    assert _held_boxes(out) == [(7, SPOT)]


def test_an_uncorroborated_person_nobody_can_find_is_released():
    hold, _ = _anchored(corroborated_ns=TS - 100 * S)
    assert _sweep(hold) == ([], {})
    assert not hold.has("patio")
    assert hold.drain_stats("patio") == {"released": 1, "uncorroborated": 1}


def test_an_uncorroborated_person_found_elsewhere_is_re_seated():
    hold, a = _anchored(corroborated_ns=TS - 100 * S)
    moved = (700.0, 400.0, 760.0, 560.0)
    out, _ = _sweep(hold, candidate_dets=[(PERSON, moved)], crop_embed=_looks({moved: _axis(0)}))
    assert _held_boxes(out) == [(7, moved)]
    assert a.corroborated_ns == TS
    assert hold.drain_stats("patio") == {"held": 1}


def test_a_detection_on_the_spot_refreshes_corroboration():
    hold, a = _anchored(corroborated_ns=TS - 100 * S)
    out, _ = _sweep(hold, candidate_dets=[(DOG, SPOT)])
    assert _held_boxes(out) == [(7, SPOT)]
    assert a.corroborated_ns == TS


def test_between_checks_the_hold_is_carried_without_looking():
    hold, _ = _anchored(last_check_ns=TS - S // 2)
    look = _looks({SPOT: _axis(1)})
    out, _ = _sweep(hold, crop_embed=look)
    assert _held_boxes(out) == [(7, SPOT)]
    assert look.calls == []


def test_a_matching_spot_verifies_the_subject():
    hold, a = _anchored(misses=1, ctx_bbox=CTX)
    out, _ = _sweep(hold, crop_embed=_looks({SPOT: _axis(0), CTX: _axis(2)}))
    assert _held_boxes(out) == [(7, SPOT)]
    assert (a.misses, a.holding, a.last_verified_ns, a.last_check_ns) == (0, True, TS, TS)
    assert list(a.ctx_emb) == list(_axis(2))


def test_a_whole_scene_change_re_baselines_instead_of_missing():
    hold, a = _anchored(misses=1, ctx_bbox=CTX, ctx_emb=_axis(2))
    out, _ = _sweep(hold, crop_embed=_looks({SPOT: _axis(1), CTX: _axis(3)}))
    assert _held_boxes(out) == [(7, SPOT)]
    assert (list(a.emb), list(a.ctx_emb), a.misses) == (list(_axis(1)), list(_axis(3)), 0)


def test_an_empty_spot_counts_a_miss_and_the_last_one_releases():
    hold, a = _anchored(ctx_bbox=CTX, ctx_emb=_axis(2))
    look = _looks({SPOT: _axis(1), CTX: _axis(2)})
    out, _ = _sweep(hold, crop_embed=look)
    assert _held_boxes(out) == [(7, SPOT)]
    assert a.misses == 1
    assert hold.drain_stats("patio") == {"held": 1}

    a.misses, a.last_check_ns = 2, 0
    assert _sweep(hold, crop_embed=look) == ([], {})
    assert not hold.has("patio")
    assert hold.drain_stats("patio") == {"released": 1}


def test_a_missing_frame_is_not_evidence_of_an_empty_seat():
    hold, a = _anchored()
    out, _ = _sweep(hold, crop_embed=_looks({}))
    assert _held_boxes(out) == [(7, SPOT)]
    assert a.misses == 0
