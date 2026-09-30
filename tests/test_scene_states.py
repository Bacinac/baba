"""What a scene region is allowed to be.

`scene_regions.states` was carried around and never read: the classifier took
whichever prototype sat nearest, whatever its label. A probe capture — one row,
deleted a second later, but appended to the evaluator's in-memory set by the
capture path — was enough for three regions to commit `__probe__` as their
state and file transitions for it. Nothing downstream broke, because a state
that is neither `present` nor `empty` is inert in the place registry, but the
regions spent four minutes describing themselves with a word the operator had
never declared.
"""

import numpy as np
from baba_state_evaluator.__main__ import _classify


def _unit(*v):
    a = np.array(v, dtype=np.float32)
    return a / np.linalg.norm(a)


EMPTY = _unit(1, 0, 0)
PRESENT = _unit(0, 1, 0)
STRAY = _unit(0.99, 0.14, 0)  # near-identical to the live frame, as a probe is


def test_a_label_the_region_never_declared_cannot_win():
    protos = [("empty", EMPTY), ("present", PRESENT), ("__probe__", STRAY)]
    label, dist, per_state = _classify(STRAY, protos, ["empty", "present"])
    assert label == "empty"
    assert "__probe__" not in per_state
    # And the honest distance to what it IS allowed to be, not to the stray.
    assert dist > 0.0


def test_a_declared_third_state_still_wins_when_it_is_nearest():
    """West P4 carries `blinded` for the nights its lens is webbed over, and
    that is a state the operator declared — it must be reachable."""
    protos = [("empty", EMPTY), ("present", PRESENT), ("blinded", STRAY)]
    label, _, per_state = _classify(
        STRAY, protos, ["empty", "present", "blinded"])
    assert label == "blinded"
    assert set(per_state) == {"empty", "present", "blinded"}


def test_no_declared_states_falls_back_to_every_prototype():
    """A region with an empty `states` column classifies as it always did,
    rather than silently becoming unclassifiable."""
    protos = [("empty", EMPTY), ("present", PRESENT)]
    label, _, _ = _classify(PRESENT, protos, None)
    assert label == "present"


def test_a_verdict_has_to_win_by_more_than_a_coin_toss():
    """Shed's P1 spent the dark hour of 31.08 choosing between empty=0.355 and
    present=0.375 — at 05:15 between 0.388 and 0.389 — then committed the winner
    and released a car that had not moved. A distance inside the margin says the
    picture resembles something trained; it does not say the region can tell its
    states apart.

    Real answers win by a mile, measured on the same regions the same morning:
    west P2's actual departure was 0.164 against 0.727, its daytime read 0.033
    against 0.600, a blinded frame 0.000 against 0.533.
    """
    from baba_state_evaluator.__main__ import verdict

    coin_toss = {"empty": 0.355, "present": 0.375}
    assert verdict("empty", 0.355, coin_toss, 0.40) == "unknown"
    assert verdict("present", 0.388, {"present": 0.388, "empty": 0.389}, 0.40) == "unknown"

    departure = {"empty": 0.164, "present": 0.727}
    assert verdict("empty", 0.164, departure, 0.40) == "empty"

    # Beyond the margin nothing is an answer, however far ahead it is.
    assert verdict("empty", 0.51, {"empty": 0.51, "present": 0.99}, 0.40) == "unknown"
    # A region with one state has no runner-up to beat.
    assert verdict("open", 0.11, {"open": 0.11}, 0.40) == "open"


def test_blindness_is_measured_and_appearance_cannot_reach_it():
    """West P1 watches a black car two metres from the lens. Taught as an
    appearance, `blinded` was a set of black crops, and on 31.08 under 117 luma
    of daylight the region crossed present<->blinded twelve times between 13:19
    and 14:50 — a dark bonnet filling the frame really is nearer a black frame
    than the yard behind it. Each flip made the place unreleasable, because a
    blinded view abstains from the departure vote.

    The bands were measured off the recordings of the night of 02.-03.09: 0.0
    to 2.0 with the floodlight off, 9.9 to 63 in every frame carrying a picture.
    """
    from baba_state_evaluator.__main__ import read_region

    protos = [("empty", EMPTY), ("present", PRESENT)]
    states = ["present", "empty", "blinded"]

    # Floodlight off: the region says so whatever the nearest prototype is.
    label, _, _ = read_region(0.1, PRESENT, protos, states, 0.40)
    assert label == "blinded"

    # Daylight over a black car: the 31.08 flip cannot happen again.
    label, _, _ = read_region(33.0, PRESENT, protos, states, 0.40)
    assert label == "present"

    # The dimmest frame that still carried a picture, west P2 at dawn.
    label, _, _ = read_region(9.9, EMPTY, protos, states, 0.40)
    assert label == "empty"


def test_glare_on_the_lens_withholds_the_verdict():
    """15.09 05:45, west P1: rain on the glass under the IR floodlight read
    `empty` over a car that never moved, and the place was released. Two samples
    8 s apart under that glare differed by 11.4-42.2; clean scenes over seven
    days stayed under 3 in 977 of 1008 pairs."""
    from baba_state_evaluator.__main__ import read_region

    protos = [("empty", EMPTY), ("present", PRESENT)]
    states = ["present", "empty", "blinded"]

    label, _, _ = read_region(30.0, EMPTY, protos, states, 0.40, 11.4)
    assert label == "unknown"
    label, _, _ = read_region(30.0, EMPTY, protos, states, 0.40, 2.9)
    assert label == "empty"
    # No previous sample to compare with is not evidence of glare.
    label, _, _ = read_region(30.0, EMPTY, protos, states, 0.40, None)
    assert label == "empty"
    # Darkness is still darkness, whatever the frames do.
    label, _, _ = read_region(0.1, EMPTY, protos, states, 0.40, 40.0)
    assert label == "blinded"


def test_steadiness_compares_structure_not_noise():
    from baba_state_evaluator.__main__ import region_thumb, unsteadiness

    rng = np.random.default_rng(0)
    scene = rng.integers(40, 200, size=(135, 461, 3), dtype=np.uint8)
    noisy = np.clip(scene.astype(np.int16) + rng.integers(-6, 7, size=scene.shape), 0, 255).astype(np.uint8)
    lit = scene.copy()
    lit[20:110, 60:400] = 250

    base = region_thumb(scene)
    assert unsteadiness(base, region_thumb(noisy)) < 3
    assert unsteadiness(base, region_thumb(lit)) > 6
    assert unsteadiness(None, base) is None
    assert unsteadiness(base, region_thumb(scene[:, :200])) is None


def test_a_region_that_never_declared_blinded_never_says_it():
    """Shed keeps its own illumination and was measured at 14.1-20.6 spread
    right through the hour west reads 0.0. A state the operator did not declare
    is not a state, in the dark as much as in the light."""
    from baba_state_evaluator.__main__ import read_region

    protos = [("empty", EMPTY), ("present", PRESENT)]
    label, _, _ = read_region(0.0, EMPTY, protos, ["present", "empty"], 0.40)
    assert label == "empty"


def test_blinded_prototypes_are_never_judged_against():
    """The label survives in `states` because it is still what the region
    commits; only the teaching is gone. A leftover prototype must not be able to
    win on looks."""
    from baba_state_evaluator.__main__ import read_region

    protos = [("empty", EMPTY), ("present", PRESENT), ("blinded", STRAY)]
    label, _, per_state = read_region(
        30.0, STRAY, protos, ["present", "empty", "blinded"], 0.40
    )
    assert "blinded" not in per_state
    assert label != "blinded"


def test_a_blind_crop_may_not_become_a_reference():
    """The measurement existed on the read side and was thrown away on the
    write side, so the nightly harvest at 05:15 — inside the hour west's left
    third is mean 0.0 — could enrol a black frame as what a place looks like.
    A region taught that way then says `empty` at every dawn."""
    from baba_state_evaluator.__main__ import teachable

    assert teachable("empty", 40.0)
    assert teachable("present", 9.9)
    assert not teachable("empty", 0.0)
    assert not teachable("present", 2.0)


def test_the_one_class_whose_references_show_nothing_is_exempt():
    from baba_state_evaluator.__main__ import teachable

    assert teachable("blinded", 0.0)
