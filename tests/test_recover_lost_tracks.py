"""What a coasting tracker's tail means when a track is rebuilt from samples.

The gate pass on 11.09 left fourteen samples over nine seconds, but the man
was in frame for six: the last eight repeat one detection's confidence to the
bit while the parked-ghost latch held his box on an empty driveway. Counting
those as presence would file a visit that outlasts the visitor.
"""

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from recover_lost_tracks import evidence_end

T0 = datetime(2026, 9, 11, 9, 1, 18, tzinfo=UTC)


def _s(*confidences):
    return [(T0 + timedelta(seconds=i), c) for i, c in enumerate(confidences)]


def test_the_repeated_tail_does_not_extend_the_visit():
    """Measured shape of the real pass: five distinct readings, then a frozen
    one repeated until the tracker gave up."""
    end, n = evidence_end(_s(0.95, 0.92, 0.93, 0.92, 0.96, 0.94, 0.935, 0.935,
                             0.935, 0.935, 0.935, 0.935, 0.935, 0.935))
    assert end == T0 + timedelta(seconds=6)
    assert n == 7


def test_a_track_that_never_repeats_keeps_its_whole_span():
    end, n = evidence_end(_s(0.90, 0.80, 0.70))
    assert end == T0 + timedelta(seconds=2)
    assert n == 3


def test_one_sample_is_a_visit_of_zero_length_not_an_error():
    end, n = evidence_end(_s(0.9))
    assert end == T0
    assert n == 1
