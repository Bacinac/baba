"""A re-read that rescues nothing has to say what stood in the way.

The pass is one-shot per track — it stamps face_native_at whether it found a
bigger face or not — so a track that stays unidentifiable gets exactly one
chance to leave a trace. It used to leave none, and the three cases that need
three different answers were indistinguishable from outside: the face was not
in the recording either (optics), the segment had rotated off the disk
(retention), or the read itself broke (a defect). Measured 2026-09-09: patio
re-read 114 person tracks in seven days, made five identifiable, and said
nothing about the other 109.
"""

import asyncio
import inspect

import baba_embedder.native_faces as nf


class FakePool:
    def __init__(self, samples):
        self._samples = samples
        self.executed = []

    async def fetch(self, *_a, **_k):
        return self._samples

    async def execute(self, sql, *args):
        self.executed.append((sql, args))


def reader(samples):
    r = nf.NativeFaceReader.__new__(nf.NativeFaceReader)
    r._pool = FakePool(samples)
    r._media_root = nf.Path("/media")
    return r


TRACK = {"id": "t1", "slug": "patio", "downscale_max_edge": 1280, "face_px": 41.0}


def test_every_silent_exit_of_the_reader_names_itself():
    src = inspect.getsource(nf.NativeFaceReader._read_face)
    # A bare `return None` here is the whole defect: it reaches the caller as
    # "nothing happened" and leaves the track with no trace at all.
    assert "return None" not in src
    # Every way out of the reader carries a name. Counting occurrences would
    # break the moment one of them gains a second call site, so assert what
    # actually matters: no exit is anonymous.
    for name in ("_NO_FILE", "_NO_OPEN", "_NO_TIMEBASE", "_NO_FRAME",
                 "_NO_CROP", "_NO_FACE"):
        assert name in src, name


def test_a_track_with_no_footage_left_says_so():
    r = reader([])
    best, why = asyncio.run(r._reread(TRACK))
    assert best is None
    assert why == [nf._NO_SEGMENT]


def test_a_missing_segment_is_told_apart_from_a_faceless_crop():
    samples = [
        {"id": "s1", "captured_at": None, "bbox": [0, 0, 1, 1], "face_px": 41.0,
         "path": "segments/patio/a.mp4", "started_at": None},
        {"id": "s2", "captured_at": None, "bbox": [0, 0, 1, 1], "face_px": 41.0,
         "path": "segments/patio/b.mp4", "started_at": None},
    ]
    r = reader(samples)
    answers = iter([nf._NO_FILE, nf._NO_FACE])
    r._read_face = lambda *_a, **_k: next(answers)
    best, why = asyncio.run(r._reread(TRACK))
    assert best is None
    assert why == [nf._NO_FILE, nf._NO_FACE]


def test_a_face_no_bigger_than_the_ring_is_not_a_rescue():
    samples = [
        {"id": "s1", "captured_at": None, "bbox": [0, 0, 1, 1], "face_px": 41.0,
         "path": "segments/patio/a.mp4", "started_at": None},
    ]
    r = reader(samples)
    # The recording gave a face, but no more of it than the ring already had.
    r._read_face = lambda *_a, **_k: (object(), object(), 38.0, 0.9, 0.11, True)
    best, why = asyncio.run(r._reread(TRACK))
    assert best is None
    assert why == [nf._NO_GAIN]
