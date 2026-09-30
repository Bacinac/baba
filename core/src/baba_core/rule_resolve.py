"""Resolving the detection rule for one (camera, class) pair.

The detector runs this on every frame to decide what it publishes. The api runs
it to tell the UI what a camera's rules actually ARE, because the card that
shows them used to work it out itself — and got it wrong in the one case that
matters. Its fallback for a class with no global row was a made-up 0.25, so a
per-camera override on a class the operator never enabled globally displayed as
"enabled, 0.25" while the detector was dropping it unconditionally. The screen
for tuning a camera was the screen least able to tell you what the camera does.

Third module of this shape, after `detection_gate` and `phantom_match`, and for
the same reason each time: when two parts of the system answer the same
question, they must call the same function or they will eventually answer
differently — and the one that is wrong is always the one facing the operator.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["ClassRule", "Effective", "resolve"]


@dataclass(frozen=True, slots=True)
class ClassRule:
    """A rules row. `None` on an override field means "inherit the global"."""

    enabled: bool | None
    min_confidence: float | None
    min_box_pct: float | None


@dataclass(frozen=True, slots=True)
class Effective:
    """What actually applies, plus why — the reason is what the UI needs to
    stop presenting a dropped class as a configured one."""

    enabled: bool
    min_confidence: float
    min_box_pct: float
    # "unconfigured" — the global table is empty, so nothing is filtered yet.
    # "global"       — the global row applies as-is.
    # "override"     — a per-camera row changes it.
    # "not-allowed"  — no global row for this class: dropped, and a per-camera
    #                  override CANNOT rescue it. The operator must enable the
    #                  class globally first.
    source: str


def resolve(
    global_rule: ClassRule | None,
    camera_rule: ClassRule | None,
    *,
    any_global_rules: bool,
) -> Effective:
    """The one resolution order, for both the detector and the api.

    An empty global table is "no opinion", not "block everything": a fresh
    deploy must not sit blind while the operator finds the settings page. Once
    anything is saved the table is non-empty and allowlist semantics apply.
    """
    if not any_global_rules:
        return Effective(True, 0.0, 0.0, "unconfigured")
    if global_rule is None:
        return Effective(False, 1.0, 0.0, "not-allowed")
    enabled = bool(global_rule.enabled)
    min_conf = float(global_rule.min_confidence or 0.0)
    min_box = float(global_rule.min_box_pct or 0.0)
    if camera_rule is None:
        return Effective(enabled, min_conf, min_box, "global")
    if camera_rule.enabled is not None:
        enabled = camera_rule.enabled
    if camera_rule.min_confidence is not None:
        min_conf = float(camera_rule.min_confidence)
    if camera_rule.min_box_pct is not None:
        min_box = float(camera_rule.min_box_pct)
    return Effective(enabled, min_conf, min_box, "override")
