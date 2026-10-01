"""Plate reading is BYOM: its weights are not permissively licensed, so an
install that has not named them must not fetch them on its own.
"""

from baba_core.plate_stack import make_plate_stack


def test_unnamed_models_fetch_nothing(tmp_path):
    assert make_plate_stack("", "", models_dir=tmp_path) is None
    assert not (tmp_path / "cache").exists()
