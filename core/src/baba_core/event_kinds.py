"""Every kind of row the `events` table holds.

The services write these in their own SQL, and the web names each one; the
gate holds the web's words to this list, so a new kind starts here.
"""

from typing import Literal

EventKind = Literal[
    "doorbell_press",
    "left_with_vehicle",
    "object_parked",
    "object_unparked",
    "scene_state_change",
    "track_finalized",
    "vehicle_arrived",
    "vehicle_left",
    "zone_dwell",
    "zone_enter",
    "zone_exit",
]
