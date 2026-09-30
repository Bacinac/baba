"""Defects the 2026-09-06 audit found in the tracker, pinned.

The tracker was 3990 lines with one test import and the detector 2185 with
none, which is why every one of these lived. Each test below is a defect that
was reaching production, not a hypothetical.
"""



from baba_tracker import anchor_hold as ah
from baba_tracker import phantom_spots as ps
from baba_tracker.__main__ import MOTION_ACTIVE, MotionTracker, _advance

# --- the anchor's context patch ------------------------------------------

def test_a_wide_central_box_has_no_context_patch():
    """`_context_bbox` returns None when neither neighbour fits. Two of the
    four sites handed that straight to a function whose first statement
    unpacks it; the TypeError left the anchor call and was swallowed by the
    handler's blanket except, discarding the whole tick for that camera."""
    frame_w, frame_h = 960, 1280
    wide_central = (300.0, 600.0, 760.0, 1100.0)
    assert ah._context_bbox(wide_central, frame_w, frame_h) is None


def test_a_narrow_box_still_gets_one():
    assert ah._context_bbox((100.0, 600.0, 200.0, 800.0), 960, 1280) is not None


# --- the motion state machine --------------------------------------------

def _motion() -> MotionTracker:
    return MotionTracker(
        static_move_ratio=0.08,
        static_move_min_px=4.0,
        static_window_ms=8000,
        park_threshold_ms=60000,
    )


def test_peek_reports_without_testifying():
    """While Norfair coasts it returns the same detection with a predicted
    box. Feeding those to the state machine filled the stillness history with
    identical centroids, so a subject that had LEFT read as perfectly still,
    was promoted to `stationary`, and left a parked ghost at its exit box."""
    m = _motion()
    m.update(1, (10.0, 10.0, 50.0, 90.0), "person", 1_000_000_000)
    before = m.peek(1)
    for _ in range(50):
        assert m.peek(1) == before


def test_peek_of_an_unknown_track_reads_active():
    assert _motion().peek(999)[0] == MOTION_ACTIVE


def test_all_four_stillness_knobs_reach_a_live_state_machine():
    """`static_window_ms` and `static_move_min_px` are editable in the same
    panel as the other two and were read once, at construction — and a
    MotionTracker is rebuilt only when its camera is evicted, which never
    happens on an always-on camera."""
    m = _motion()
    m.set_params(0.2, 30000, 20000, 9.0)
    assert m._static_move_ratio == 0.2
    assert m._park_threshold_ms == 30000
    assert m._static_window_ms == 20000
    assert m._static_move_min_px == 9.0


def test_demotion_clears_every_ghost_at_that_spot():
    """A ghost describes a SPOT, and the registry exists because a flickering
    static object cycles through track ids — so N entries per physical spot is
    the steady state. Popping one by track id left the rest holding the spot
    parked for the remaining two hours of their own TTL."""
    m = _motion()
    box = (100.0, 100.0, 200.0, 200.0)
    for tid in (1, 2, 3):
        m._ghosts[tid] = (box, "car", 0, 1_000_000_000, False)
    assert m._forget_ghosts_at(box, "car", 2) == 3
    assert m._ghosts == {}


def test_demotion_leaves_a_different_spot_alone():
    m = _motion()
    m._ghosts[1] = ((100.0, 100.0, 200.0, 200.0), "car", 0, 1_000_000_000, False)
    m._ghosts[2] = ((700.0, 700.0, 800.0, 800.0), "car", 0, 1_000_000_000, False)
    assert m._forget_ghosts_at((100.0, 100.0, 200.0, 200.0), "car", 1) == 1
    assert list(m._ghosts) == [2]


# --- norfair's initialization delay ---------------------------------------

class _FakeTracker:
    def __init__(self):
        self.initialization_delay = None
        self.reid_distance_threshold = None
        self.periods: list[int] = []
        self.tracked_objects: list = []

    def update(self, detections=None, period=1):
        self.periods.append(period)
        return []


def test_one_observation_does_not_earn_an_id():
    """The delay was a constant in REAL frames (frame_rate // 5 = 3) while
    Norfair counts in the NOMINAL frames `_advance` replays: a camera at 2 fps
    hands over period 8, so `8 <= 3` was false at every birth and no object
    was ever initializing. Norfair only offers INITIALIZING objects to its
    re-identification stage, so the whole OSNet re-attach — wired, and exposed
    in the UI as `reid_lost_seconds` — could never fire."""
    t = _FakeTracker()
    _advance(t, [], dt_s=0.5, frame_rate=15)          # 2 fps
    assert t.initialization_delay == 8 == t.periods[-1]
    _advance(t, [], dt_s=0.25, frame_rate=15)         # 4 fps
    assert t.initialization_delay == 4 == t.periods[-1]


def test_the_reid_threshold_is_pushed_live():
    t = _FakeTracker()
    _advance(t, [], dt_s=0.5, frame_rate=15, reid_threshold=0.45)
    assert t.reid_distance_threshold == 0.45


# --- the phantom birth registry -------------------------------------------

def test_the_spot_being_created_survives_the_cap():
    """Eviction sorts by births descending and drops the tail. A spot created
    a moment ago has none, so past the cap it was always the one dropped: the
    registry could never learn a new spot again, and the caller went on
    holding a reference to a spot no longer in the list."""
    reg = ps.PhantomSpots.__new__(ps.PhantomSpots)
    cam = ps._CamState()
    for i in range(ps._MAX_SPOTS_PER_CAMERA + 1):
        cam.spots.append(ps.Spot(
            class_name="person", bbox=(0.0, 0.0, 0.1, 0.1),
            births=10 + i, movers=0, first_birth_s=0.0, last_birth_s=0.0,
        ))
    newborn = ps.Spot(class_name="person", bbox=(0.5, 0.5, 0.6, 0.6),
                      births=0, movers=0, first_birth_s=0.0, last_birth_s=0.0)
    cam.spots.append(newborn)
    reg._evict_if_needed(cam, keep=newborn)
    assert newborn in cam.spots


def test_without_keep_the_lowest_evidence_still_goes():
    reg = ps.PhantomSpots.__new__(ps.PhantomSpots)
    cam = ps._CamState()
    for i in range(ps._MAX_SPOTS_PER_CAMERA + 2):
        cam.spots.append(ps.Spot(
            class_name="person", bbox=(0.0, 0.0, 0.1, 0.1),
            births=i, movers=0, first_birth_s=0.0, last_birth_s=0.0,
        ))
    reg._evict_if_needed(cam)
    assert len(cam.spots) == ps._MAX_SPOTS_PER_CAMERA
    assert min(s.births for s in cam.spots) > 1


# --- who counts as having arrived under their own power -------------------

def test_a_phantom_cannot_renew_a_strangers_provenance():
    """`real_mover` gates whether a track is reported as a person at all, and a
    spot with one banked mover is one the phantom registry may never suppress.

    It used to be a bare bool on the parked ghost: whatever was born on that
    spot inherited it AND wrote it straight back, so a corner producing a
    static false positive renewed a stranger's provenance for ever. Measured on
    patio: three tracks travelling 2.3, 0.7 and 2.0 px against box diagonals of
    114-202 px, all carrying it, on a spot with 10,282 births and 3,587 banked
    movers — reporting a person to DIDA over an empty terrace."""
    m = _motion()
    earned_at = 1_000_000_000
    fresh = (( 0.0, 0.0, 10.0, 10.0), "person", 0, earned_at, earned_at)
    assert m._ghost_mover(fresh, earned_at + 60 * 1_000_000_000)

    stale = earned_at + (m._MOVER_PROVENANCE_MS + 60_000) * 1_000_000
    assert not m._ghost_mover(fresh, stale), (
        "provenance has to lapse, or a corner keeps a person's flag for ever")


def test_a_spot_nobody_ever_walked_carries_nothing():
    m = _motion()
    never = ((0.0, 0.0, 10.0, 10.0), "person", 0, 5, 0)
    assert not m._ghost_mover(never, 10)
    assert not m._ghost_mover(None, 10)


# --- the newborn under a variable period ----------------------------------

def _tracker_with_reid():
    from baba_tracker.__main__ import _make_reid_distance, _norfair_tracker

    return _norfair_tracker(
        distance_threshold=0.7,
        initialization_delay=0,
        hit_counter_max=150,
        past_detections_length=5,
        reid_distance_function=_make_reid_distance(lambda: 0.3),
        reid_distance_threshold=0.3,
        reid_hit_counter_max=300,
    )


def _standing_car(embedding):
    import numpy as np
    from baba_tracker.__main__ import _DetData
    from norfair import Detection

    return Detection(
        points=np.array([[947.0, 201.0], [1871.0, 536.0]], dtype=np.float32),
        scores=np.array([0.6, 0.6], dtype=np.float32),
        embedding=embedding,
        data=_DetData(class_id=2, class_name="car", confidence=0.6, birth_eligible=True),
    )


def _same_look():
    import numpy as np

    return np.full(512, 1.0 / np.sqrt(512), dtype=np.float32)


def test_a_standing_car_is_tracked_from_its_second_frame_after_a_restart():
    """The first frame after a restart has no predecessor and is worth p = 1,
    so Norfair gave its newborn one nominal frame of life; the next real frame
    at idle rate cost seven. Dead before its second observation, never
    initialised, and offered to re-identification anyway, it became the object
    every later newborn of the same car merged into — `merge` hands back two
    frames of life, which dies again, and nothing was ever emitted. West after
    the 08:56 deploy of 15.09: two cars in plain view, thirteen minutes, zero
    tracks; the departing car was acquired only at the far gate, once its look
    had drifted past the re-ID threshold."""
    t = _tracker_with_reid()
    look = _same_look()
    # The ingestor's own idle spacing (measured 0.385–0.67 s between frames).
    intervals = [1 / 15, 0.446, 0.537, 0.491, 0.592, 0.672, 0.499, 0.474, 0.559, 0.486]
    ids = set()
    for i, dt in enumerate(intervals):
        out = _advance(t, [_standing_car(look)], dt_s=dt, frame_rate=15, reid_threshold=0.3)
        if i >= 1:
            assert len(out) == 1, f"tick {i}: a car seen on every frame was not emitted"
            ids.add(out[0].id)
    assert len(ids) == 1


def test_a_track_seen_on_every_frame_never_runs_out_a_reid_countdown():
    """A newborn's counter dipped to zero on the way to its second observation,
    which armed Norfair's re-ID countdown on a track that was then matched on
    every single frame — and Norfair drops a track when that countdown runs
    out, whatever its hit counter says. A parked car died every
    `reid_lost_seconds` and came back as a new id."""
    t = _tracker_with_reid()
    look = _same_look()
    ids = set()
    for i in range(120):  # a minute at 2 fps, three times the re-ID window
        out = _advance(t, [_standing_car(look)], dt_s=0.5, frame_rate=15, reid_threshold=0.3)
        if i >= 1:
            assert len(out) == 1, f"tick {i}: the standing car vanished"
            ids.add(out[0].id)
    assert len(ids) == 1

