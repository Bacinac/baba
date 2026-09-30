"""Incident replay: what the proposed thresholds WOULD have done.

The AI writes a confident paragraph and a number. That is an argument, not
evidence, and the operator is being asked to change how their house is watched
on the strength of it. This module turns the argument into something you can
look at: the detector's raw, pre-gate detections for the window the incident
covers, run through the gate twice — once with the thresholds in force, once
with the proposed ones — over the same frames the recording already holds.

Two properties make it trustworthy rather than merely plausible:

  * The detections are the LIVE ones. No second inference pass, no re-decoding
    the recording (which on this hardware would enter through BT.601 while the
    model's baked graph expects BT.709 — same footage, different scores, a
    comparison that looks authoritative and isn't).
  * The gate is the LIVE gate — `baba_core.detection_gate`, imported, not
    reimplemented. A replay that drifts from production argues for a change
    that then behaves differently.

What comes back is per-frame boxes for both rule sets plus the counts that
actually decide it: how many births each side produces, per class. Recovering
a person you were losing is the win; the phantom cars that arrive with it are
the cost, and both are on screen before anything is applied.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import nats
from baba_core.detection_gate import GateRules, classify, size_pct
from baba_core.wire import SUBJECT_DETECTOR_TRACE

log = logging.getLogger(__name__)

# How much of the incident to replay. The watcher aggregates 15 minutes; showing
# all of it would be a quarter-hour of footage to sit through, and the counts
# under the video must describe exactly the footage above them — a box window
# that outran the clip would summarise seconds the operator never gets to
# watch, the one failure mode that would make this actively misleading. 45 s is
# a deliberate reading budget now, no longer the clip cutter's ceiling: that
# was raised to _CLIP_MAX_ENC_S = 240 so a whole visit arrives as one file.
REPLAY_SPAN_S = 45.0
# Frames handed to the browser. At the cameras' 4 fps a 45 s span is ~180, so
# this is headroom rather than a real cap.
MAX_REPLAY_FRAMES = 600
_TRACE_REPLY_TIMEOUT_S = 10.0


@dataclass(frozen=True, slots=True)
class ClassRules:
    """Effective thresholds for one class on one camera, both sides."""

    current: GateRules
    proposed: GateRules


def _merge(global_row: Any, camera_row: Any) -> GateRules:
    """Global floor with the per-camera override folded in — NULL inherits.
    Mirrors DetectionRules.effective's resolution order."""
    enabled = bool(global_row["enabled"])
    min_conf = float(global_row["min_confidence"])
    min_box = float(global_row["min_box_pct"] or 0.0)
    if camera_row is not None:
        if camera_row["enabled"] is not None:
            enabled = bool(camera_row["enabled"])
        if camera_row["min_confidence"] is not None:
            min_conf = float(camera_row["min_confidence"])
        if camera_row["min_box_pct"] is not None:
            min_box = float(camera_row["min_box_pct"])
    return GateRules(enabled=enabled, min_confidence=min_conf, min_box_pct=min_box)


async def resolve_rules(
    pool, camera_id: UUID, changes: list[Any]
) -> tuple[dict[str, ClassRules], float | None]:
    """Current rules per class for this camera, plus the same set with the
    suggested changes applied. Returns `(rules_by_class, maintain_conf)`.

    Only `camera_rule` changes move a threshold; `camera` changes (park_seconds,
    stillness_ratio) live in the tracker and cannot be shown as boxes on a frame
    — the caller reports those separately rather than pretending the picture
    covers them."""
    globals_ = {
        r["class_name"]: r
        for r in await pool.fetch(
            "SELECT class_name, enabled, min_confidence, min_box_pct FROM detector_global_rules"
        )
    }
    per_cam = {
        r["class_name"]: r
        for r in await pool.fetch(
            "SELECT class_name, enabled, min_confidence, min_box_pct "
            "FROM camera_detection_rules WHERE camera_id = $1",
            camera_id,
        )
    }
    cam = await pool.fetchrow("SELECT maintain_conf FROM cameras WHERE id = $1", camera_id)
    maintain_conf = float(cam["maintain_conf"]) if cam and cam["maintain_conf"] is not None else None

    overrides: dict[str, dict[str, float]] = {}
    for ch in changes:
        if ch.target != "camera_rule" or not ch.class_name:
            continue
        overrides.setdefault(ch.class_name, {})[ch.field] = ch.value

    out: dict[str, ClassRules] = {}
    for class_name, grow in globals_.items():
        current = _merge(grow, per_cam.get(class_name))
        ov = overrides.get(class_name)
        if ov:
            proposed = GateRules(
                enabled=current.enabled,
                min_confidence=float(ov.get("min_confidence", current.min_confidence)),
                min_box_pct=float(ov.get("min_box_pct", current.min_box_pct)),
            )
        else:
            proposed = current
        out[class_name] = ClassRules(current=current, proposed=proposed)
    return out, maintain_conf


# A class the global table has never heard of is dropped outright — the
# allowlist semantics DetectionRules applies when a class is missing.
_UNKNOWN = ClassRules(
    current=GateRules(enabled=False, min_confidence=1.0, min_box_pct=0.0),
    proposed=GateRules(enabled=False, min_confidence=1.0, min_box_pct=0.0),
)


async def fetch_trace(nc, camera_slug: str, start_ns: int, end_ns: int) -> dict[str, Any]:
    """Pull the detector's rolling pre-gate detections for this window."""
    payload = {
        "camera_slug": camera_slug,
        "start_ns": start_ns,
        "end_ns": end_ns,
        "max_frames": MAX_REPLAY_FRAMES,
    }
    try:
        resp = await nc.request(
            SUBJECT_DETECTOR_TRACE, json.dumps(payload).encode(), timeout=_TRACE_REPLY_TIMEOUT_S
        )
    except nats.errors.NoRespondersError:
        raise RuntimeError("detector is not running — no trace to replay") from None
    except (TimeoutError, nats.errors.TimeoutError):
        raise RuntimeError("detector did not answer the trace request") from None
    reply = json.loads(resp.data)
    if "error" in reply:
        raise RuntimeError(f"detector trace failed: {reply['error']}")
    return reply


def build_comparison(
    trace: dict[str, Any],
    rules: dict[str, ClassRules],
    maintain_conf: float | None,
) -> dict[str, Any]:
    """Run the live gate over the traced detections under both rule sets.

    Boxes come back NORMALISED (0-1) so the browser can draw them over a clip
    of any rendered size without knowing the source resolution."""
    frames_out: list[dict[str, Any]] = []
    tally: dict[str, dict[str, int]] = {}

    def _bump(class_name: str, side: str, key: str) -> None:
        tally.setdefault(class_name, {})[f"{side}_{key}"] = (
            tally.setdefault(class_name, {}).get(f"{side}_{key}", 0) + 1
        )

    for f in trace.get("frames", []):
        fw, fh = int(f["w"]), int(f["h"])
        if fw <= 0 or fh <= 0:
            continue
        cur_boxes: list[dict[str, Any]] = []
        new_boxes: list[dict[str, Any]] = []
        for class_name, conf, x1, y1, x2, y2 in f["d"]:
            bw = x2 - x1
            bh = y2 - y1
            pct = size_pct(bw, bh, fw, fh)
            cr = rules.get(class_name, _UNKNOWN)
            box = {
                "c": class_name,
                "p": round(float(conf), 3),
                "x": round(x1 / fw, 4),
                "y": round(y1 / fh, 4),
                "w": round(bw / fw, 4),
                "h": round(bh / fh, 4),
            }
            for side, gate, sink in (
                ("current", cr.current, cur_boxes),
                ("proposed", cr.proposed, new_boxes),
            ):
                reason, birth = classify(
                    gate, confidence=conf, size_pct=pct, maintain_conf=maintain_conf
                )
                if reason is not None:
                    continue
                sink.append({**box, "b": birth})
                _bump(class_name, side, "published")
                if birth:
                    _bump(class_name, side, "births")
        frames_out.append({"t": int(f["t_ns"]), "cur": cur_boxes, "new": new_boxes})

    summary = [
        {
            "class_name": class_name,
            "current_published": counts.get("current_published", 0),
            "current_births": counts.get("current_births", 0),
            "proposed_published": counts.get("proposed_published", 0),
            "proposed_births": counts.get("proposed_births", 0),
        }
        for class_name, counts in sorted(tally.items())
    ]
    summary.sort(
        key=lambda s: abs(s["proposed_births"] - s["current_births"]),
        reverse=True,
    )
    return {"frames": frames_out, "summary": summary}
