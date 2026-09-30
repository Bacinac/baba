"""What the roster says about a scene region.

The state snapshot publishes two things a consumer has to put back together:
`scenes`, keyed by region UUID, and `parked`, keyed by the PLACE. Nothing in
between said they were the same spot. DIDA could only match a region's display
name against the parked key and hope — true on this property by coincidence,
and false the day a region is named something a human would read. It is also
the only way to know that Shed's P1 and West's P1 are one place and not two.
"""

import asyncio
import json
from uuid import UUID

from baba_api.roster_bridge import RosterNatsBridge

CAM = UUID("22222222-2222-2222-2222-222222222222")
REGION = UUID("44444444-4444-4444-4444-444444444444")
GATE = UUID("55555555-5555-5555-5555-555555555555")


class FakePool:
    """Dispatches on the SQL the bridge actually issues."""

    async def fetch(self, sql, *_args):
        if "detector_global_rules" in sql:
            return [{"class_name": "car"}]
        if "camera_detection_rules" in sql:
            return []
        if "scene_regions" in sql:
            assert "place" in sql, "the roster must select the region's place"
            return [
                {"id": REGION, "camera_id": CAM, "name": "P4",
                 "states": ["empty", "present", "blinded"], "place": "P4"},
                {"id": GATE, "camera_id": CAM, "name": "Ulazna vrata",
                 "states": ["open", "closed"], "place": None},
            ]
        return [{
            "camera_id": CAM, "camera_slug": "west", "camera_name": "West",
            "camera_enabled": True, "camera_doorbell": False,
            "zone_id": None, "zone_name": None, "zone_kind": None,
            "zone_enabled": None, "rules": None,
        }]


def _scenes():
    bridge = RosterNatsBridge.__new__(RosterNatsBridge)
    bridge._pool = FakePool()
    body = json.loads(asyncio.run(bridge._build()))
    cams = body["cameras"] if isinstance(body, dict) else body
    return {s["name"]: s for s in cams[0]["scenes"]}


def test_a_region_that_watches_a_place_says_which_one():
    scenes = _scenes()
    assert scenes["P4"]["place"] == "P4"
    # The id stays the identity the state snapshot keys by; place is the join.
    assert scenes["P4"]["id"] == str(REGION)


def test_a_region_that_stands_for_itself_publishes_a_null_place():
    """Not omitted: "watches no place" is a statement, and a consumer that
    cannot tell it from a missing field has to guess again."""
    scenes = _scenes()
    assert "place" in scenes["Ulazna vrata"]
    assert scenes["Ulazna vrata"]["place"] is None


def test_the_declared_states_still_ride_along():
    """West P4 carries a third state since the spiderweb night; a consumer
    validates `scenes` against this list rather than a fixed enum."""
    assert _scenes()["P4"]["states"] == ["empty", "present", "blinded"]
