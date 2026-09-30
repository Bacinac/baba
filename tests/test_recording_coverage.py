"""What a recording segment covers, and why an open one is not forever.

A row with no `ended_at` is either the segment being written now or one the
indexer never closed. Six queries asked `ended_at IS NULL OR ended_at > x`,
which answers "it covers everything from its start onwards" — true of the
first, catastrophic for the second.

Measured 07.09: every camera carried orphan rows from 05:32. The clip cutter
takes its window offset as `start - first_segment_start`, so with an orphan
sorting first a clip at 07:19 asked ffmpeg to seek 6457 seconds into a
concatenation three minutes long. Nothing was written, every fallback failed
the same way, and the operator got a spinner that never resolved — on every
camera, not only the HEVC one.
"""

import re
from pathlib import Path

from baba_core.recordings import OPEN_SEGMENT_GRACE, covers_until_sql

ROOT = Path(__file__).resolve().parent.parent
SQL_SITES = (
    "services/api/src/baba_api/routes_recordings.py",
    "services/api/src/baba_api/routes_sightings.py",
    "services/api/src/baba_api/routes_events.py",
    "services/state-evaluator/src/baba_state_evaluator/__main__.py",
    "services/state-evaluator/src/baba_state_evaluator/plate_reader.py",
    "services/embedder/src/baba_embedder/native_faces.py",
)


def test_an_open_segment_is_bounded_by_its_own_start():
    sql = covers_until_sql()
    assert "COALESCE(ended_at" in sql
    assert f"started_at + interval '{OPEN_SEGMENT_GRACE}'" in sql


def test_it_carries_whatever_alias_the_query_uses():
    assert covers_until_sql("r.started_at", "r.ended_at") == (
        "COALESCE(r.ended_at, r.started_at + interval "
        f"'{OPEN_SEGMENT_GRACE}')"
    )


def test_no_query_treats_an_open_segment_as_unbounded():
    """The shape of the bug rather than one instance. `ended_at IS NULL` in a
    coverage test is the thing that let one abandoned row answer for any
    moment, on six different paths."""
    offenders = []
    for rel in SQL_SITES:
        body = (ROOT / rel).read_text()
        for m in re.finditer(r"ended_at IS NULL", body):
            line = body[: m.start()].count("\n") + 1
            offenders.append(f"{rel}:{line}")
    assert not offenders, (
        "an open recording is being treated as covering all time at: "
        + ", ".join(offenders)
    )


def test_the_orphan_repair_keeps_running():
    """It was spawned once at startup and the recorder runs for weeks, so a
    segment orphaned mid-run stayed open for ever."""
    body = (ROOT / "services/recorder/src/baba_recorder/supervisor.py").read_text()
    assert "_orphan_sweep_loop" in body
    assert "while not self._stopping.is_set():" in body
