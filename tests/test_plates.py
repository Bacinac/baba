"""The plate decision layer, pinned to its measured behaviour.

Every case here is an incident or a measurement from live footage, so a
refactor that "simplifies" the module has to argue with the driveway first.
"""

from baba_core.plates import (
    PlateSighting,
    PlateVote,
    bind_to_place,
    drop_fixtures,
    drop_standing,
    focus_times,
    match_gallery,
    moving_samples,
    normalise_plate,
    plate_distance,
)


def test_normalise_strips_separators_and_diacritics():
    assert normalise_plate("ZG 9420-GZ") == "ZG9420GZ"
    assert normalise_plate("žč-šđ") == "ZCSDJ"


def test_distance_exact_is_zero():
    assert plate_distance("ZG9420GZ", "ZG 9420 GZ") == 0.0


def test_confusable_substitution_is_cheap_hard_is_not():
    # 0<->O look alike under blur; the OCR's actual failure mode.
    assert plate_distance("ZG942OGZ", "ZG9420GZ") == 0.4
    # 7->4 shares no glyph group; that is real disagreement.
    assert plate_distance("ZG9320GZ", "ZG9420GZ") == 1.0


def test_partial_alignment_forgives_a_clipped_area_code():
    # The one clean reading of the 27.07 manoeuvre: '9420GZ' at confidence 1.0
    # — ZG9420GZ minus the clipped prefix. Charged as deletions it dies at 2.0;
    # aligned as a partial it is exactly the plate.
    assert plate_distance("9420GZ", "ZG9420GZ") == 2.0
    assert plate_distance("9420GZ", "ZG9420GZ", partial=True) == 0.0
    # But a stub below six characters discriminates nothing: 'GZ' is the tail
    # of half the county and must stay worthless however cleanly it aligns.
    assert plate_distance("GZ", "ZG9420GZ", partial=True) == 8.0


def test_vote_consensus_is_per_column_majority():
    v = PlateVote()
    for _ in range(26):
        v.add("ZG9420GZ")
    v.add("ZG942OGZ")  # one blurred frame does not move a 26-frame majority
    text, agreement = v.consensus()
    assert text == "ZG9420GZ"
    assert min(agreement) > 0.9


def test_gallery_refuses_a_distant_winner():
    # The measured honest outcome: the parked-oblique consensus sat 4.0 from
    # an eight-character plate, and no margin may rescue that.
    assert match_gallery("LB1234CD", {"a": "ZG9420GZ"}) is None


def test_gallery_needs_a_margin_between_candidates():
    gallery = {"marko": "ZG9420GZ", "ana": "ZG9648GZ"}
    hit = match_gallery("ZG9420GZ", gallery)
    assert hit is not None and hit.key == "marko"
    # A reading that two enrolled plates explain about equally names nobody:
    # ZG9428GZ sits 1.0 from one and 2.0 from the other — under the 1.5 margin.
    assert match_gallery("ZG9428GZ", gallery) is None


def _pass(plate_xy, car_xy, text="ZG9648GZ", width=80.0):
    """One camera's readings of `text` while the car went from car_xy[0] to [-1]."""
    return [
        PlateSighting(text=text, confidence=1.0, width=width, at=p, owner=c)
        for p, c in zip(plate_xy, car_xy, strict=True)
    ]


def test_a_plate_that_never_moves_is_not_on_the_car_that_did():
    # 16.08, shed: the arrival's box crossed the frame from (2298, 949) to
    # (1998, 212) while the plate the zone kept reading stayed bolted to the
    # car parked in the background. Seventy-seven readings, unanimous, and the
    # wrong car — which renamed the arrival, demoted the parked car's own
    # place to unknown, and overwrote west's correct reading.
    car = [(2298, 949), (2271, 562), (2015, 333), (1998, 212)]
    parked = [(1819, 373)] * 4
    kept, dropped = drop_fixtures(_pass(parked, car))
    assert kept == []
    assert [d[0] for d in dropped] == ["ZG9648GZ"]
    _, travelled, moved, n = dropped[0]
    assert n == 4 and moved == 0.0 and travelled > 700


def test_a_plate_riding_the_car_survives():
    car = [(2298, 949), (2271, 562), (2015, 333), (1998, 212)]
    plate = [(2250, 980), (2230, 600), (1980, 370), (1960, 250)]
    kept, dropped = drop_fixtures(_pass(plate, car, text="ZG9420GZ"))
    assert len(kept) == 4 and dropped == []


def test_a_car_that_stopped_gives_the_reading_up():
    # This test asserted the opposite until 05.09, on the reasoning that a
    # plate held still in front of a gate is exactly as still as one bolted to
    # the fence, so a rule with nothing to compare against should abstain.
    #
    # Abstaining means keeping, and the night of 04.09 measured the price:
    # seventeen readings of a neighbour's registration, unanimous on all eight
    # characters because the car wearing it had been parked for four minutes,
    # taken while the tracked car reversed into a bay two spaces over. It named
    # the wrong car and put that name on the wrong place.
    #
    # A stopped car's own plate is recoverable — the place it stops in gets
    # read where it stands. A wrong name is not recoverable, so this is the
    # side to fail on.
    car = [(1000, 500), (1002, 501), (999, 500), (1001, 499)]
    plate = [(980, 520)] * 4
    kept, dropped = drop_fixtures(_pass(plate, car))
    assert kept == []
    assert len(dropped) == 1


def test_the_scenery_goes_and_the_real_plate_stays_in_the_same_pass():
    # Both plates in one crop is the normal case on a driveway, and the point
    # of judging each string on its own geometry rather than taking whichever
    # the detector returned first.
    car = [(2298, 949), (2271, 562), (2015, 333), (1998, 212)]
    sightings = _pass([(1819, 373)] * 4, car) + _pass(
        [(2250, 980), (2230, 600), (1980, 370), (1960, 250)], car, text="ZG9420GZ"
    )
    kept, dropped = drop_fixtures(sightings)
    assert {s.text for s in kept} == {"ZG9420GZ"}
    assert [d[0] for d in dropped] == ["ZG9648GZ"]


def test_a_track_with_no_samples_keeps_what_it_read():
    # Nothing knows where that car was, so nothing here may claim the plate is
    # someone else's. The zone-only path exists for exactly those tracks.
    kept, dropped = drop_fixtures(
        [
            PlateSighting(text="ZG9648GZ", confidence=1.0, width=80.0, at=(1819, 373))
            for _ in range(4)
        ]
    )
    assert len(kept) == 4 and dropped == []


def _seen(plate_xy, text="ZG9420GZ", width=80.0):
    """Readings with no owner — what a read triggered by a place filling has."""
    return [
        PlateSighting(text=text, confidence=1.0, width=width, at=p)
        for p in plate_xy
    ]


def test_a_plate_that_crossed_the_window_is_the_arrival():
    # 26.08 16:12:33, west: the plate grew 75 -> 124 px as the car came up the
    # approach, and its position swept most of the zone. No track existed for
    # this arrival at all, which is the whole reason this path exists.
    kept, dropped = drop_standing(
        _seen([(220, 300), (300, 340), (410, 390), (560, 450)])
    )
    assert len(kept) == 4 and dropped == []


def test_a_plate_standing_in_the_zone_is_not_the_arrival():
    # An ALPR zone is a fixed rectangle over a driveway: whatever parks inside
    # it reads perfectly on every frame, forever. With no car box to measure
    # against, the plate's own stillness is the only thing that can refuse it —
    # and it must, or somebody else's arrival takes the parked car's name.
    kept, dropped = drop_standing(_seen([(400, 380), (402, 381), (399, 379)]))
    assert kept == []
    text, moved, n = dropped[0]
    assert text == "ZG9420GZ" and n == 3 and moved < 5


def test_the_arrival_is_kept_and_the_scenery_dropped_in_one_window():
    sightings = (
        _seen([(220, 300), (300, 340), (410, 390), (560, 450)], text="ZG9420GZ")
        + _seen([(880, 210)] * 4, text="ZG9648GZ")
    )
    kept, dropped = drop_standing(sightings)
    assert {s.text for s in kept} == {"ZG9420GZ"}
    assert [d[0] for d in dropped] == ["ZG9648GZ"]


def test_a_single_reading_cannot_testify_and_is_dropped():
    # One frame is exactly what a fixture looks like when the scan was short.
    # `drop_fixtures` may abstain here because a track established the car was
    # there; nothing establishes that on this path, so the benefit of the
    # doubt would be an invitation to name a place after the scenery.
    kept, dropped = drop_standing(_seen([(400, 380)]))
    assert kept == [] and dropped[0][2] == 1


def _samples(boxes, t0=0):
    """(bbox, captured_at) samples one second apart."""
    from datetime import datetime, timedelta
    base = datetime(2026, 8, 22, 17, 34, 43)
    return [{"bbox": b, "captured_at": base + timedelta(seconds=t0 + i)}
            for i, b in enumerate(boxes)]


def test_a_car_that_stopped_is_not_read_after_it_stopped():
    """22.08 shed: the box crossed the frame for 13 s and then sat still for 8,
    resting over the car parked behind. Neighbour-differencing counted the
    parked box's own width breathing as movement, so the read window ran to the
    end of the track and every reading came from the static tail — where the
    only legible plate was the neighbour's."""
    travelling = [[1148 - i * 20, 298 - i * 20, 1280, 523 - i * 20] for i in range(13)]
    resting = [[881 + (i % 2), 5, 1104 - (i % 2), 212] for i in range(8)]
    samples = _samples(travelling + resting)
    moving = moving_samples(samples)
    assert moving, "the approach itself must still count as movement"
    # Coming to rest is itself a move; everything after it is not. The window
    # may therefore reach the moment the car stopped and no further.
    settled = samples[len(travelling)]["captured_at"]
    assert moving[-1]["captured_at"] <= settled
    assert all(t <= settled for t in focus_times(samples, 120))
    assert len([s for s in moving if s["captured_at"] > settled]) == 0


def test_a_parked_box_that_breathes_is_not_travelling():
    """The distance-from-a-reference rule died here: a resting box swinging a
    quarter of its own width clears any threshold on every frame, and the
    reference follows it there, so all twenty static samples read as travel —
    the same failure neighbour-differencing had, at a larger amplitude. A step
    that reverses every frame does not."""
    travelling = [[1148 - i * 20, 298 - i * 20, 1280, 523 - i * 20] for i in range(13)]
    width, swing = 223, 60
    resting = [
        [881 + (swing if i % 2 else 0), 5, 881 + width + (swing if i % 2 else 0), 212]
        for i in range(20)
    ]
    samples = _samples(travelling + resting)
    settled = samples[len(travelling)]["captured_at"]
    moving = moving_samples(samples)
    assert moving, "the approach itself must still count as movement"
    assert [s for s in moving if s["captured_at"] > settled] == []


def test_a_car_that_reverses_into_a_space_still_travels_after_it_turns():
    """Distance from the track's first box stops growing once the car reaches
    its farthest point, so a rule built on that ends the scan window there —
    and on West the plate faces the camera during the reverse, not before it."""
    forward = [[400 + i * 25, 200, 700 + i * 25, 450] for i in range(10)]
    backing = [[625 - i * 25, 200, 925 - i * 25, 450] for i in range(1, 10)]
    samples = _samples(forward + backing)
    moving = moving_samples(samples)
    assert moving[-1]["captured_at"] == samples[-1]["captured_at"]


def test_a_car_coming_head_on_still_counts_as_moving():
    """Its centre barely moves while its box grows — which is why width is
    compared at all. Removing that term outright would blind the reader to the
    one approach that presents a plate properly."""
    boxes = [[900 - i * 12, 300 - i * 12, 900 + i * 12, 300 + i * 12] for i in range(1, 12)]
    assert len(moving_samples(_samples(boxes))) >= 8




def _sighting(text, at, width=83.0, owner=None):
    return PlateSighting(text=text, confidence=1.0, width=width, at=at, owner=owner)


def test_fixture_is_dropped_even_when_the_car_stood_still():
    """04.09 22:32: seventeen readings of a neighbour's plate at the same pixel
    while the car being read reversed into a bay two spaces over. The car had
    not travelled, so the comparison abstained — and abstaining kept it."""
    still = [_sighting("ZG9648GZ", (1752.0 + i * 0.4, 350.0), owner=(2100.0 + i, 400.0))
             for i in range(17)]
    kept, dropped = drop_fixtures(still)
    assert kept == []
    assert [d[0] for d in dropped] == ["ZG9648GZ"]


def test_a_stopped_car_still_gives_up_its_own_plate():
    """The case the rule protects: a car halted at a gate, its own plate moving
    with it as it inches forward."""
    rolling = [_sighting("ZG9420GZ", (400.0 + i * 30.0, 500.0), owner=(600.0 + i * 30.0, 520.0))
               for i in range(8)]
    kept, dropped = drop_fixtures(rolling)
    assert len(kept) == 8
    assert dropped == []


SHED = {
    # shed P2 and, for the competition, a bay drawn where P1 sits in that frame.
    "P2": (0.527 * 2560, 0.105 * 1440, 0.678 * 2560, 0.288 * 1440),
    "P1": (0.80 * 2560, 0.20 * 1440, 0.95 * 2560, 0.35 * 1440),
}


def test_the_plate_of_the_car_standing_here_is_bound_to_this_place():
    """Measured 05.09 08:56 on shed: 0.71 of its own widths outside P2."""
    kept, rejected = bind_to_place([_sighting("ZG9648GZ", (1795.0, 362.0))], "P2", SHED)
    assert len(kept) == 1
    assert rejected == []


def test_the_plate_one_bay_over_is_not():
    """Same frame, same second: 8.2 widths away, and nearer to the other bay."""
    kept, rejected = bind_to_place(
        [_sighting("ZGX3729P", (2152.0, 385.0), width=51.0)], "P2", SHED)
    assert kept == []
    assert rejected and rejected[0][1] == "P1"


def test_a_place_with_no_region_on_this_camera_claims_nothing():
    kept, rejected = bind_to_place([_sighting("ZG9648GZ", (1795.0, 362.0))], "P2", {})
    assert kept == [] and rejected == []


WEST = {
    # West's own three, in its 4096x1152 pixels. P1 is nearly three times the
    # width of P2 and sits directly below it, which is what makes nearest-wins
    # necessary: grown far enough to reach its own car's plate, P1 covers P2
    # outright.
    "P4": (0.328 * 4096, 0.185 * 1152, 0.405 * 4096, 0.375 * 1152),
    "P1": (0.60 * 4096, 0.73 * 1152, 0.84 * 4096, 0.98 * 1152),
    "P2": (0.635 * 4096, 0.41 * 1152, 0.71 * 4096, 0.55 * 1152),
}


def test_the_wider_place_does_not_swallow_the_one_above_it():
    plate = _sighting("ZG9648GZ", (2750.0, 640.0), width=120.0)
    kept, _ = bind_to_place([plate], "P2", WEST)
    assert len(kept) == 1
    kept, rejected = bind_to_place([plate], "P1", WEST)
    assert kept == [] and rejected[0][1] == "P2"


def test_a_car_in_its_own_place_is_bound_there():
    plate = _sighting("ZG9420GZ", (2900.0, 1000.0), width=140.0)
    assert len(bind_to_place([plate], "P1", WEST)[0]) == 1
    assert bind_to_place([plate], "P2", WEST)[0] == []


def test_a_bay_under_the_lens_still_reads_as_an_arrival():
    """West P1 sits beneath the camera: a car reaching it turns and stops within
    seconds, so its plate never sweeps the widths an approach across the frame
    does. On 11.09 07:59 the resident's own plate travelled 106 px against a
    107 px bar — four clean readings binned by one pixel, and the place stood
    `unknown` all morning with the car in plain sight. The bar belongs just
    above the detector's jitter, not near the travel of a real approach."""
    # The real numbers: 106 px of travel on a 75 px plate. The old bar stood at
    # 1.5 widths = 112 px, so these four readings were binned; the measured bar
    # sits at 0.3 widths = 22 px, which 106 clears five times over.
    kept, dropped = drop_standing(
        _seen([(1420, 980), (1460, 1000), (1490, 1020), (1512, 1032)], width=75.0)
    )
    assert len(kept) == 4 and dropped == [], "106 px of travel is not standing still"


def test_the_fixture_this_guard_exists_for_still_loses():
    """The neighbour's plate parked inside the zone: it does not drift, it reads
    perfectly on every frame, and lowering the bar must not hand it somebody
    else's arrival. Measured against a real fixture — 2 px of wander."""
    kept, dropped = drop_standing(
        _seen([(400, 380), (402, 381), (399, 379), (401, 380)], width=70.0)
    )
    _, moved, n = dropped[0]
    assert kept == [] and n == 4 and moved < 5


def test_a_vote_can_assemble_a_string_no_frame_read():
    """12.09, west P1: a guest car under the carport and seven readings of its
    roof trim. Every reading a different string, so the per-position majority
    is a plate nobody saw — and unenrolled, it went on the place badge."""
    v = PlateVote()
    for text in ("C00002", "K06166", "C0666", "C16066", "CHA166", "W1100", "ZZ3774"):
        v.add(text)
    text, _ = v.consensus()
    assert v.seen(text) == 0


def test_an_unmatched_plate_names_a_place_only_as_it_was_read():
    from uuid import uuid4

    from baba_state_evaluator.plate_reader import PlateReader

    assert PlateReader._assembled("C06066", 0, None, "west")
    assert PlateReader._assembled("C06066", 1, None, "west")
    assert not PlateReader._assembled("ZG45671", 2, None, "west")
    # The gallery, not the frames, is what vouches for an enrolled plate.
    assert not PlateReader._assembled("ZG942OGZ", 0, uuid4(), "west")
