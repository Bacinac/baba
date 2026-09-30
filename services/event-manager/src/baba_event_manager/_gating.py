"""Zone x class / motion-gate resolution (pure functions)."""

from __future__ import annotations

from baba_event_manager.zones import ZoneClassRule, _Zone

# Sentinel returned by `_zone_class_rule` when the zone has an explicit
# enabled_classes set that does NOT contain the track's class.  Distinct
# from `None` (which means "no allowlist — all classes pass") so the
# caller can tell "reject" from "no rule, inherit".
_ZONE_CLASS_REJECTED: ZoneClassRule = ZoneClassRule()


# Default motion gate by zone kind. Parking/Entry/Interest zones are
# almost never meant to fire events for stationary objects (parked cars
# are the canonical UX-killer — bbox jitter wobbles the centre across
# the polygon boundary every frame). Restricted zones intentionally
# DO fire on stationary objects (intruder standing still still counts).
# Generic stays legacy ("fire anything") so existing operator setups
# are not silently changed by this rollout.
#
# Per-class `motion_gate` in zones.rules overrides this default when
# set; this map is the fallback when the operator hasn't customised.
_ZONE_KIND_DEFAULT_MOTION_GATE: dict[str, str | None] = {
    "parking": "moving_only",
    "entry": "moving_only",
    "interest": "moving_only",
    "restricted": None,
    "generic": None,
}


def _effective_motion_gate(
    zone: _Zone | None,
    rule: ZoneClassRule | None,
) -> str | None:
    """Resolve the active motion_gate for this zone × class. Per-class
    rule wins when set, else the zone-kind default kicks in, else legacy
    (no gating)."""
    if rule is not None and rule.motion_gate:
        return rule.motion_gate
    if zone is None:
        return None
    return _ZONE_KIND_DEFAULT_MOTION_GATE.get(zone.kind.lower())


# The motion gate exists for parked VEHICLES: a static bbox whose centre
# wobbles across a polygon edge would otherwise emit an endless enter/exit
# stream. A person is the opposite case. Someone who stops moving inside an
# Interest or Entry zone is not noise — that is loitering, the single most
# report-worthy thing a camera can see, and gating it away is exactly the
# failure this system exists to not have (a seated person on the patio
# generated zone events on arrival and then vanished from Activity for hours).
# Measured 2026-07-23: 68 patio person tracks suppressed as "off-zone" while
# three people sat in plain view.
_MOTION_GATE_EXEMPT_CLASSES = frozenset({"person"})


def _motion_gate_allows(
    gate: str | None, motion_state: str | None, class_name: str | None = None
) -> bool:
    """Decide whether a track in `motion_state` should generate zone
    events under `gate`. Unknown gates (typo / missing) and missing
    motion_state both fall through to "allow" — fail-open keeps a
    broken config from silently dropping events."""
    if not gate:
        return True
    if (class_name or "").lower() in _MOTION_GATE_EXEMPT_CLASSES:
        return True
    state = (motion_state or "active").lower()
    if gate == "moving_only":
        return state == "active"
    return True


def _zone_class_rule(
    zone: _Zone | None,
    class_name: str,
) -> ZoneClassRule | None:
    """Resolve the zone × class rule.

    Returns:
      - `None`                    : zone has no class allowlist; track passes,
                                    no per-class refinement.
      - `_ZONE_CLASS_REJECTED`    : zone HAS an allowlist and this class
                                    is not on it; track is rejected.
      - `ZoneClassRule(...)`      : zone has an allowlist and this class
                                    is on it; caller applies the fields.
    """
    if zone is None or zone.class_rules is None:
        return None
    rule = zone.class_rules.get(class_name)
    if rule is None:
        return _ZONE_CLASS_REJECTED
    return rule

