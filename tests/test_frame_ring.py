"""The shared-memory ring between the ingestor and every reader of its frames.

Writer and readers run in one process here, against a real POSIX segment.
"""

import uuid

import numpy as np
import pytest
from baba_core.frame_ring import (
    PIXEL_FORMAT_NV12,
    PIXEL_FORMAT_RGB,
    FrameRingReader,
    FrameRingWriter,
)

W, H, N = 64, 48, 4


def rgb(seq: int, w: int = W, h: int = H) -> np.ndarray:
    return ((np.arange(h * w * 3, dtype=np.uint32).reshape(h, w, 3) + seq) % 251).astype(np.uint8)


def nv12(seq: int) -> np.ndarray:
    return ((np.arange((H + H // 2) * W, dtype=np.uint32).reshape(H + H // 2, W) + seq) % 251).astype(
        np.uint8
    )


@pytest.fixture
def ring():
    slug = f"test_{uuid.uuid4().hex[:12]}"
    writers: list[FrameRingWriter] = []

    def make(*, width=W, height=H, fmt=PIXEL_FORMAT_RGB) -> FrameRingWriter:
        w = FrameRingWriter(slug, width, height, n_frames=N, pixel_format=fmt)
        writers.append(w)
        return w

    make.slug = slug
    yield make
    for w in writers:
        w.close()


def test_a_pushed_frame_reads_back_by_its_sequence_as_a_private_copy(ring):
    w = ring()
    w.push(rgb(7), 7, 700)
    got = FrameRingReader(ring.slug).get_by_sequence(7)
    assert (got.sequence, got.pts_ns, got.pixel_format, got.width, got.height) == (7, 700, "rgb", W, H)
    assert np.array_equal(got.pixels, rgb(7))
    for s in range(8, 8 + N):
        w.push(rgb(s), s, 100 * s)
    assert np.array_equal(got.pixels, rgb(7))


def test_nv12_keeps_its_logical_picture_size(ring):
    ring(fmt=PIXEL_FORMAT_NV12).push(nv12(3), 3, 300)
    got = FrameRingReader(ring.slug).get_by_sequence(3)
    assert (got.pixel_format, got.width, got.height, got.pixels.shape) == ("nv12", W, H, (H + H // 2, W))
    assert np.array_equal(got.pixels, nv12(3))


def test_nv12_refuses_odd_dimensions(ring):
    with pytest.raises(ValueError, match="even"):
        ring(width=W + 1, fmt=PIXEL_FORMAT_NV12)


def test_the_ring_holds_exactly_its_last_n_frames(ring):
    w = ring()
    reader = FrameRingReader(ring.slug)
    assert reader.get_latest() is None
    for s in range(1, 11):
        w.push(rgb(s), s, 100 * s)
    held = {s for s in range(1, 11) if reader.get_by_sequence(s) is not None}
    assert held == set(range(11 - N, 11))
    latest = reader.get_latest()
    assert latest.sequence == 10 and np.array_equal(latest.pixels, rgb(10))


def test_a_reader_attached_after_the_ring_wrapped_reads_the_right_slot(ring):
    # push() once published write_index over the header's frame_bytes field,
    # so a reader attaching late multiplied slots by the frame counter.
    w = ring()
    for s in range(1, 3 * N):
        w.push(rgb(s), s, 100 * s)
    got = FrameRingReader(ring.slug).get_by_sequence(3 * N - 2)
    assert np.array_equal(got.pixels, rgb(3 * N - 2))


def test_a_frame_of_the_wrong_shape_is_dropped_not_written(ring):
    w = ring()
    w.push(rgb(1), 1, 100)
    w.push(rgb(2, w=W // 2), 2, 200)
    reader = FrameRingReader(ring.slug)
    assert reader.get_by_sequence(2) is None
    assert reader.get_latest().sequence == 1


def test_a_reader_of_an_absent_ring_sees_nothing():
    reader = FrameRingReader(f"test_{uuid.uuid4().hex[:12]}")
    assert reader.get_by_sequence(1) is None
    assert reader.get_latest() is None


def test_a_reader_follows_the_writer_into_a_recreated_segment(ring):
    # The old segment stays mapped after its name moves on and still holds
    # frames, so nothing about a read from it looks wrong: the latest frame is
    # always there, and a restarted writer reuses sequence numbers.
    old = ring()
    old.push(rgb(1), 1, 100)
    reader = FrameRingReader(ring.slug)
    assert reader.get_latest().width == W
    old.close()
    ring(width=2 * W).push(rgb(1, w=2 * W), 1, 100)
    latest, by_seq = reader.get_latest(), reader.get_by_sequence(1)
    assert latest.width == by_seq.width == 2 * W
    assert np.array_equal(by_seq.pixels, rgb(1, w=2 * W))


def test_a_reader_lets_go_of_a_segment_whose_writer_is_gone(ring):
    old = ring()
    old.push(rgb(1), 1, 100)
    reader = FrameRingReader(ring.slug)
    assert reader.get_latest() is not None
    old.close()
    assert reader.get_by_sequence(2) is None
    ring().push(rgb(2), 2, 200)
    assert np.array_equal(reader.get_by_sequence(2).pixels, rgb(2))


def _lap_during_copy(monkeypatch, w: FrameRingWriter, first: int) -> None:
    """The writer laps the ring between a reader's slot lookup and its copy."""
    real = np.frombuffer

    def lapping(*args, **kwargs):
        view = real(*args, **kwargs)
        monkeypatch.setattr(np, "frombuffer", real)
        for s in range(first, first + N):
            w.push(rgb(s), s, 100 * s)
        return view

    monkeypatch.setattr(np, "frombuffer", lapping)


def test_a_slot_overwritten_mid_copy_is_discarded_not_mislabelled(ring, monkeypatch):
    w = ring()
    w.push(rgb(1), 1, 100)
    reader = FrameRingReader(ring.slug)
    _lap_during_copy(monkeypatch, w, 2)
    assert reader.get_by_sequence(1) is None


def test_the_latest_frame_overwritten_mid_copy_comes_back_whole(ring, monkeypatch):
    w = ring()
    w.push(rgb(1), 1, 100)
    reader = FrameRingReader(ring.slug)
    _lap_during_copy(monkeypatch, w, 2)
    got = reader.get_latest()
    assert got.sequence == 1 + N
    assert np.array_equal(got.pixels, rgb(1 + N))
