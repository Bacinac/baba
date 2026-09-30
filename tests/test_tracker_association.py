"""The metric every detection is matched to a track by.

Norfair scores a per-pair Python callable one pair at a time and warns about
it on every camera's tracker; ours scores the whole matrix at once and must
survive the boxes Norfair's own IoU cannot.
"""

import logging

import numpy as np
from baba_tracker.__main__ import _iou_distance, _norfair_tracker


def test_iou_distance_scores_every_detection_against_every_track():
    dets = np.array([[0, 0, 10, 10], [100, 100, 110, 110]], dtype=np.float32)
    tracks = np.array([[0, 0, 10, 10], [5, 0, 15, 10], [20, 20, 30, 30]], dtype=np.float64)
    np.testing.assert_allclose(
        _iou_distance(dets, tracks),
        [[0.0, 1 - 50 / 150, 1.0], [1.0, 1.0, 1.0]],
    )


def test_a_pair_with_no_area_is_no_overlap_not_a_crash():
    """Norfair's own IoU divides 0 by 0 here, and a NaN in the matrix raises
    out of `update` — one degenerate box would cost the camera its frame."""
    flat = np.array([[5, 5, 5, 9]], dtype=np.float32)
    for est in ([[9, 9, 5, 5]], [[5, 5, 5, 5]]):
        d = _iou_distance(flat, np.array(est, dtype=np.float64))
        assert not np.isnan(d).any()
        assert d[0, 0] == 1.0


def test_the_tracker_is_built_on_the_matrix_metric(caplog):
    with caplog.at_level(logging.WARNING):
        t = _norfair_tracker(distance_threshold=0.7, hit_counter_max=15, initialization_delay=0)
    assert "scalar distance function" not in caplog.text
    assert t.distance_function.distance_function is _iou_distance
