"""What a recording segment covers, said once.

A row with no `ended_at` is one of two things: the segment being written right
now, or one the indexer never closed. Six queries asked
`ended_at IS NULL OR ended_at > x`, which answers "it covers everything from
its start to the end of time" — true of the first, catastrophic for the second.

Measured 07.09: every camera carried orphan rows from 05:32, and the clip
cutter picked the oldest of them as the segment its window opened in. The
window offset is `start - first_segment_start`, so a clip at 07:19 asked ffmpeg
to seek 6457 seconds into a concatenation three minutes long. Nothing was
written, every fallback failed the same way, and the operator got a spinner
that never resolved — on every camera, not only the HEVC one.

So an open segment covers its start plus one segment's worth of time and no
more. The one being written now is inside that; one orphaned hours ago is not.
"""

from __future__ import annotations

# A segment is 60 s. The grace is what a still-open row may still be worth: the
# live one is seconds old, and anything older than this was abandoned.
OPEN_SEGMENT_GRACE = "2 minutes"


def covers_until_sql(started: str = "started_at", ended: str = "ended_at") -> str:
    """SQL for the moment a segment stops covering, open rows included."""
    return f"COALESCE({ended}, {started} + interval '{OPEN_SEGMENT_GRACE}')"
