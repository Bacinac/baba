"""Every table that names an identity has to be told when one changes.

`presence_episodes` was not. The merge moved tracks, reference photos, place
occupancy and plate reads to the surviving identity and left presence pointing
at a gid that had ceased to exist; the delete cleared the same four and left
presence naming somebody who no longer existed anywhere. No damage had landed
yet — the first merge would have been the first loss.
"""

import re
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "services/api/src/baba_api/routes_identities"

# Every table carrying a `global_id`, taken from the live schema. A new one
# belongs in both operations below, and this list is where that is noticed.
CARRIES_IDENTITY = (
    "identity_labels",
    "identity_reference_photos",
    "place_occupancy",
    "plate_reads",
    "presence_episodes",
    "tracks",
)


def _op(path: str, name: str) -> str:
    body = (SRC / path).read_text()
    start = body.index(f"async def {name}(")
    nxt = body.find("\nasync def ", start + 1)
    return body[start:nxt if nxt != -1 else len(body)]


def test_a_merge_moves_every_table_that_names_an_identity():
    merge = _op("_merge.py", "merge_identity")
    for table in CARRIES_IDENTITY:
        assert table in merge, f"merge leaves {table} pointing at the absorbed identity"


def test_a_delete_clears_every_table_that_names_an_identity():
    delete = _op("_core.py", "delete_identity")
    for table in CARRIES_IDENTITY:
        assert table in delete, f"delete leaves {table} naming somebody who is gone"


def test_the_merge_keeps_one_open_presence_per_camera():
    """`presence_episodes` allows one open row per identity and camera. Moving
    the absorbed identity's rows across without folding the collisions would
    violate that index and abort the merge halfway."""
    merge = _op("_merge.py", "merge_identity")
    fold = re.search(r"UPDATE presence_episodes.*?departed_at IS NULL", merge, re.S)
    assert fold, "the merge moves presence without folding the open collision"
    assert "LEAST(s.present_since" in merge and "GREATEST(s.last_confirmed_at" in merge, (
        "a folded presence must cover both, or the merge shortens somebody's visit")
