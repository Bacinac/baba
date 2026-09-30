"""Scene-state evaluation service.

Classifies the persistent state of a fixed region in the frame (gate
open/closed, garage door up/down, light on/off) on a slow cadence, using
DINOv2 region embeddings nearest-neighboured against a handful of operator
captured few-shot prototypes. Transitions become `scene_state_change` events.
"""
