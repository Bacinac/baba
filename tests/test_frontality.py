"""A similarity fit cannot squash, and that is the whole measure.

The pipeline ranked a track's faces by how sure the detector was that a face
was there, which is silent on whether that face is turned toward the camera.
On a camera mounted high the crown of a head is bigger and detected more
confidently than the glance upward, so the glance lost; and what won was
sometimes not a head at all — patio carried a 117 px "face" that is a beam of
the awning, which also lifted the track over the identity floor and kept it
out of the native re-read.

Measured 2026-09-10 from both sides: of 162 curated reference portraits none
exceeds 0.29 (median 0.085, p99 0.247, max 0.269), and of the pipeline's own
accepted detections every one above it was paving, a beam or a wall.
"""

import numpy as np
import pytest
from baba_core.face import _ARCFACE_TEMPLATE, FRONTALITY_MAX, frontality_residual


def rotate(pts, deg):
    a = np.deg2rad(deg)
    R = np.array([[np.cos(a), -np.sin(a)], [np.sin(a), np.cos(a)]], np.float32)
    return (R @ pts.T).T


def test_the_template_itself_is_the_zero():
    assert frontality_residual(_ARCFACE_TEMPLATE.copy()) == pytest.approx(0.0, abs=1e-4)


@pytest.mark.parametrize("scale,deg,shift", [(1.0, 0, 0.0), (4.0, 0, 0.0),
                                             (0.3, 0, 0.0), (1.0, 25, 0.0),
                                             (2.5, -40, 300.0)])
def test_a_face_stays_a_face_under_scale_rotation_and_shift(scale, deg, shift):
    """The transform the fit is allowed to make must cost nothing — otherwise
    a small face, or a tilted head, would read as a non-face."""
    pts = rotate(_ARCFACE_TEMPLATE * scale, deg) + shift
    assert frontality_residual(pts.astype(np.float32)) == pytest.approx(0.0, abs=1e-3)


def test_squashing_is_what_the_measure_cannot_absorb():
    """Foreshortening — a head tipped away from the camera — is exactly the
    deformation a similarity cannot undo."""
    squashed = _ARCFACE_TEMPLATE.copy()
    squashed[:, 1] *= 0.35
    assert frontality_residual(squashed) > FRONTALITY_MAX


def test_incoherent_points_are_far_past_the_line():
    """The back of a head still yields five points; they just do not describe
    a face. Eyes below the mouth, nose off to one side."""
    garbage = np.array([[70.0, 95.0], [40.0, 92.0], [95.0, 60.0],
                        [45.0, 50.0], [72.0, 48.0]], np.float32)
    assert frontality_residual(garbage) > FRONTALITY_MAX


def test_the_measured_band_of_real_faces_stays_below_the_line():
    """p99 of the 162 curated portraits was 0.247 and the max 0.269; the line
    sits above both on purpose, because it may only demote, never reject."""
    assert FRONTALITY_MAX > 0.269


def test_a_bad_shape_is_no_answer_rather_than_a_wrong_one():
    assert frontality_residual(np.zeros((3, 2), np.float32)) is None
