"""A stored AI suggestion is checked again before anything acts on it.

The suggestion row is operator-visible but not operator-authored, and the
tunable ranges may have tightened since the analysis ran. A change that no
longer passes is the operator's to see as a refusal, not a server error.
"""

import pytest
from baba_api.routes_telemetry import _revalidated
from fastapi import HTTPException


def test_a_change_inside_the_whitelist_passes():
    (change,) = _revalidated([{"target": "camera", "field": "idle_fps", "value": 5}])
    assert (change.field, change.value) == ("idle_fps", 5.0)


@pytest.mark.parametrize(
    "raw",
    [
        {"target": "camera", "field": "enabled", "value": 0},
        {"target": "camera", "field": "idle_fps", "value": 99},
        {"target": "camera_rule", "field": "min_confidence", "value": 0.5},
        {"target": "camera_rule", "class_name": "person", "field": "min_confidence", "value": 0.9},
        {"target": "zone", "field": "idle_fps", "value": 5},
        {"target": "camera", "field": "idle_fps", "value": "five"},
    ],
)
def test_a_change_that_no_longer_passes_is_refused_not_crashed(raw):
    with pytest.raises(HTTPException) as e:
        _revalidated([raw])
    assert e.value.status_code == 400
    assert e.value.detail.startswith("suggested change rejected: ")
