"""Capture hooks: stamp manual tuning into the light-profile table + journal.

Every settings decision made by hand (a rule threshold slider, the
stillness card) is recorded under the camera's CURRENT measured
illumination band, exactly like an AI-applied incident suggestion —
otherwise the next profile switch back into this band would silently
revert the operator's work. Each hook writes one `camera_setting_changes`
journal row (full before/after snapshots) and refreshes the profile row.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from baba_core.light_profiles import capture_profile, record_change, snapshot_live

log = logging.getLogger(__name__)


async def journal_rule_change(
    pool, camera_id, class_name: str, before_rule: dict[str, Any] | None
) -> None:
    """Journal + profile-stamp after a per-camera detection-rule upsert or
    delete. `before_rule` is the pre-write row ({min_confidence, enabled,
    min_box_pct}) or None if the rule didn't exist."""
    try:
        async with pool.acquire() as conn, conn.transaction():
            cond = await conn.fetchval(
                "SELECT light_condition FROM cameras WHERE id = $1", camera_id
            )
            if cond is None:
                return
            after = await snapshot_live(conn, camera_id)
            before = json.loads(json.dumps(after))
            if before_rule is None:
                before["rules"].pop(class_name, None)
            else:
                before["rules"][class_name] = before_rule
            await record_change(
                conn,
                camera_id,
                source="manual",
                light_condition=cond,
                before=before,
                after=after,
            )
            await capture_profile(conn, camera_id, cond)
    except Exception:
        # The live write already succeeded — a capture failure must not
        # turn the operator's edit into an error response.
        log.exception("profile capture failed for camera %s", camera_id)


async def journal_stillness_change(pool, camera_id, before_value: float) -> None:
    await _journal_scalar_change(pool, camera_id, "stillness_ratio", before_value)


async def journal_maintain_change(pool, camera_id, before_value: float) -> None:
    await _journal_scalar_change(pool, camera_id, "maintain_conf", before_value)


async def _journal_scalar_change(pool, camera_id, field: str, before_value: float) -> None:
    """Journal + profile-stamp a single scalar per-camera setting (stillness,
    maintain, …). Mirrors journal_rule_change but for a top-level snapshot key."""
    try:
        async with pool.acquire() as conn, conn.transaction():
            cond = await conn.fetchval(
                "SELECT light_condition FROM cameras WHERE id = $1", camera_id
            )
            if cond is None:
                return
            after = await snapshot_live(conn, camera_id)
            before = {**after, field: float(before_value)}
            await record_change(
                conn,
                camera_id,
                source="manual",
                light_condition=cond,
                before=before,
                after=after,
            )
            await capture_profile(conn, camera_id, cond)
    except Exception:
        log.exception("profile capture failed for camera %s", camera_id)
