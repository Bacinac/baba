"""Per-lighting-band setting profiles + settings change journal.

The correlation table (`camera_setting_profiles`, migration 052) maps
(camera, illumination band) → validated settings:

    {"stillness_ratio": 0.15,
     "rules": {"person": {"min_confidence": 0.4, "enabled": true,
                          "min_box_pct": 0.0}, ...}}

Filling is INCIDENT-DRIVEN: a profile row is written only when someone
made a real tuning decision under those conditions — an AI-suggested
apply, a manual edit, or a revert. Capture is always a full snapshot of
the live lighting-sensitive settings, so a profile row is complete by
construction and apply_settings() can safely run as a FULL sync (rules
absent from the profile are deleted).

Every mutation also lands in `camera_setting_changes` with before/after
snapshots — the journal behind one-click revert.

Live tables stay the single source the pipeline reads; DB triggers
(cameras_notify, detection_rules_notify) propagate every write here to
the detector/tracker exactly like an operator edit.
"""

from __future__ import annotations

import json
from typing import Any

# Luma cut points for the color bands (measured mean Y, 0-255). Profiles
# are keyed by band NAME, so these can be re-tuned without a migration.
_BAND_EDGES: list[tuple[str, float]] = [("dark", 40.0), ("dim", 90.0), ("normal", 150.0)]
# A band flip needs the luma to sit this far beyond the shared boundary —
# hysteresis so a value hovering at a cut point doesn't flap profiles.
_BAND_MARGIN = 8.0


def classify_light(luma: float, ir_ratio: float, current: str | None = None) -> str | None:
    """Illumination band for one telemetry bucket. IR mode wins outright
    (monochrome is its own regime regardless of level). For color, the
    current band is sticky within ±_BAND_MARGIN of its boundaries; returns
    None when there's no basis to classify (no samples this window)."""
    if ir_ratio >= 0.5:
        return "ir"
    if luma < 0:
        return None

    def band(v: float) -> str:
        for name, edge in _BAND_EDGES:
            if v < edge:
                return name
        return "bright"

    candidate = band(luma)
    if current in (None, "ir") or current == candidate:
        return candidate
    # Hysteresis: stay in the current color band while luma is within
    # ±_BAND_MARGIN of that band's [lower, upper) range.
    names = [n for n, _ in _BAND_EDGES] + ["bright"]
    edges = [e for _, e in _BAND_EDGES]
    if current not in names:
        return candidate
    idx = names.index(current)
    lower = edges[idx - 1] if idx > 0 else float("-inf")
    upper = edges[idx] if idx < len(edges) else float("inf")
    if lower - _BAND_MARGIN <= luma < upper + _BAND_MARGIN:
        return current
    return candidate


async def snapshot_live(conn, camera_id) -> dict[str, Any]:
    """Complete lighting-sensitive settings of a camera as currently live."""
    cam = await conn.fetchrow(
        "SELECT stillness_ratio, maintain_conf FROM cameras WHERE id = $1", camera_id
    )
    stillness = cam["stillness_ratio"] if cam is not None else None
    rows = await conn.fetch(
        "SELECT class_name, min_confidence, enabled, min_box_pct "
        "FROM camera_detection_rules WHERE camera_id = $1",
        camera_id,
    )
    return {
        "stillness_ratio": float(stillness) if stillness is not None else None,
        "maintain_conf": float(cam["maintain_conf"]) if cam is not None else None,
        "rules": {
            r["class_name"]: {
                "min_confidence": (
                    float(r["min_confidence"]) if r["min_confidence"] is not None else None
                ),
                "enabled": r["enabled"],
                "min_box_pct": (
                    float(r["min_box_pct"]) if r["min_box_pct"] is not None else None
                ),
            }
            for r in rows
        },
    }


async def load_profile(conn, camera_id, condition: str) -> dict[str, Any] | None:
    raw = await conn.fetchval(
        "SELECT settings FROM camera_setting_profiles WHERE camera_id = $1 AND condition = $2",
        camera_id,
        condition,
    )
    if raw is None:
        return None
    return json.loads(raw) if isinstance(raw, str) else raw


async def capture_profile(conn, camera_id, condition: str) -> dict[str, Any]:
    """Stamp the CURRENT live settings as the profile for `condition` —
    called right after a tuning decision (AI apply, manual edit, revert)
    so the correlation table always holds the latest validated choice for
    the conditions it was made under."""
    snapshot = await snapshot_live(conn, camera_id)
    await conn.execute(
        """
        INSERT INTO camera_setting_profiles (camera_id, condition, settings)
        VALUES ($1, $2, $3::jsonb)
        ON CONFLICT (camera_id, condition)
        DO UPDATE SET settings = EXCLUDED.settings, updated_at = now()
        """,
        camera_id,
        condition,
        json.dumps(snapshot),
    )
    return snapshot


async def apply_settings(conn, camera_id, settings: dict[str, Any]) -> None:
    """Write a settings snapshot into the live tables. FULL sync: rules not
    in the snapshot are deleted (snapshots are complete by construction).
    DB triggers NOTIFY the detector/tracker like any operator edit."""
    if settings.get("stillness_ratio") is not None:
        await conn.execute(
            "UPDATE cameras SET stillness_ratio = $2 WHERE id = $1",
            camera_id,
            settings["stillness_ratio"],
        )
    if settings.get("maintain_conf") is not None:
        await conn.execute(
            "UPDATE cameras SET maintain_conf = $2 WHERE id = $1",
            camera_id,
            settings["maintain_conf"],
        )
    rules: dict[str, Any] = settings.get("rules") or {}
    await conn.execute(
        "DELETE FROM camera_detection_rules WHERE camera_id = $1 AND NOT (class_name = ANY($2))",
        camera_id,
        list(rules.keys()),
    )
    for cls, r in rules.items():
        await conn.execute(
            """
            INSERT INTO camera_detection_rules
                (camera_id, class_name, min_confidence, enabled, min_box_pct)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (camera_id, class_name) DO UPDATE
            SET min_confidence = EXCLUDED.min_confidence,
                enabled        = EXCLUDED.enabled,
                min_box_pct    = EXCLUDED.min_box_pct
            """,
            camera_id,
            cls,
            r.get("min_confidence"),
            r.get("enabled"),
            r.get("min_box_pct"),
        )


async def record_change(
    conn,
    camera_id,
    *,
    source: str,
    light_condition: str,
    before: dict[str, Any],
    after: dict[str, Any],
    incident_id=None,
) -> None:
    """Journal one settings mutation with full before/after snapshots —
    the row one-click revert reads its `before` from."""
    await conn.execute(
        """
        INSERT INTO camera_setting_changes
            (camera_id, source, incident_id, light_condition, before, after)
        VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb)
        """,
        camera_id,
        source,
        incident_id,
        light_condition,
        json.dumps(before),
        json.dumps(after),
    )
