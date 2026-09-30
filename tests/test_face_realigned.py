"""NULL means nobody asked, and reading it as failure erases the history.

The residual (098) asks whether five landmarks FIT the ArcFace template. A head
of hair satisfies that, so a crown and a face turned sideways share one band and
geometry cannot part them. Warping the crop onto the template and handing it
back to the same detector can: measured 2026-09-10 over 242 hand-labelled patio
crops, 0 of 70 ears, profiles and crowns survive it, against 161 of 162 enrolled
portraits.

Every row written before migration 099 carries NULL. If any ranking or any
`face_px` measurement ever reads that NULL as "did not survive", every track in
the archive loses the face it was named by in one migration. So the ordering has
to keep three states — survived, not measured, did not survive — and the px
measurement has to admit everything not KNOWN to be bad. That is the invariant
here; it is SQL, so it is guarded at the source.
"""

import inspect
import re
from pathlib import Path

from baba_core.face import set_canonical_face
from baba_embedder import native_faces as nf

SRC = Path(__file__).resolve().parent.parent / "services"
EVENT_MANAGER = SRC / "event-manager/src/baba_event_manager/__main__.py"
EMBEDDER = SRC / "embedder/src/baba_embedder/__main__.py"
REFERENCE_PHOTOS = SRC / "api/src/baba_api/routes_identities/_reference_photos.py"
CANONICAL = inspect.getsource(set_canonical_face)

# The three-state ordering, whitespace-insensitive.
THREE_STATE = re.compile(
    r"CASE\s+WHEN\s+(?:s\.)?face_realigned\s+THEN\s+0\s+"
    r"WHEN\s+(?:s\.)?face_realigned\s+IS\s+NULL\s+THEN\s+1\s+"
    r"ELSE\s+2\s+END",
    re.IGNORECASE,
)


def _text(p):
    return p.read_text(encoding="utf-8")


def test_the_canonical_face_pick_keeps_all_three_states():
    """Survived first, unmeasured second, known-bad last — never a bare
    boolean, which would sort NULL with one extreme or the other. Finalize and
    the native re-read both file the track's face through the one statement."""
    assert THREE_STATE.search(CANONICAL)
    assert "set_canonical_face(conn, track_id," in _text(EVENT_MANAGER)
    assert "set_canonical_face(self._pool," in _text(Path(nf.__file__))


def test_the_native_reread_ranks_by_the_same_three_states():
    """The re-reader picks which samples are worth going back to the recording
    for by the same ordering that then files one of them as the track's face."""
    assert len(THREE_STATE.findall(_text(Path(nf.__file__)))) == 1


def test_face_px_admits_everything_not_known_to_be_bad():
    """`IS NOT FALSE`, never `IS TRUE`: the second spelling drops every
    pre-099 row and takes the identity floor down with it."""
    assert "face_realigned IS NOT FALSE" in CANONICAL
    assert "face_realigned IS TRUE" not in CANONICAL
    assert "face_realigned = true" not in CANONICAL.lower()


def test_no_fallback_reaches_past_the_check_to_the_whole_track():
    """The hole this closes was live for an hour: the residual's fallback --
    "if every sample is implausible, take the best of all of them" -- is right
    for a soft, mostly-NULL signal and wrong for this one. Reached past the
    alignment check it handed the identity floor an ear's 68 px, and 53 of the
    370 tracks that hold nothing but non-faces went on naming people through
    it. Every fallback rung has to carry the check, and the last rung is 0 --
    not NULL, which would also drop the track out of the native re-read.
    """
    rungs = [m.group(1) for m in re.finditer(r"max\(face_px\)(.*?)\)", CANONICAL, re.S)]
    assert len(rungs) == 2
    for window in rungs:
        assert "face_realigned IS NOT FALSE" in window, window[:120]
    assert re.search(r"AND face_realigned IS NOT FALSE\),(?:\s*--[^\n]*)*\s*0\s*\)", CANONICAL)


def test_the_curated_evidence_path_is_the_one_that_excludes():
    """Everywhere else this demotes. Here it filters, because what comes out is
    offered as the evidence a name may be given from — and an ear must not
    reach the approve button. Still `IS NOT FALSE`, so history is not wiped."""
    src = _text(REFERENCE_PHOTOS)
    assert "AND s.face_realigned IS NOT FALSE" in src


def test_every_face_sample_written_carries_the_measure():
    """A sample persisted without it is indistinguishable from pre-099
    history, so the gate silently stops applying to new rows."""
    embedder = _text(EMBEDDER)
    assert "face_realigned)" in embedder
    assert "face_realigneds[i]," in embedder
    reread = _text(Path(nf.__file__))
    assert "face_realigned = $8" in reread


def test_the_reader_returns_the_measure_alongside_the_vector():
    """It is measured where the aligned crop is still in hand. Recomputing it
    later would mean reading the JPEG back and re-running the detector on a
    lossy copy of what the model actually saw."""
    src = inspect.getsource(nf.NativeFaceReader._read_face)
    assert "detector.detect(aligned) is not None" in src
