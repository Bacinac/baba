"""Telemetry incidents: list, AI analyze, apply/dismiss.

The event-manager's watchers open rows in `telemetry_incidents` when a
per-camera pattern trips (rate pinned active without real activity, rule
drop spike, class-flip storm, track churn). This module is the operator's
side of the loop:

- GET  /telemetry/incidents            — list (open + recent history)
- POST /telemetry/incidents/{id}/analyze — AI verdict: the model gets the
  incident evidence, a 30-minute telemetry summary, the camera's current
  rules/settings and a live snapshot, and answers "is this justified, and
  if not, which knob moves" as a constrained JSON suggestion.
- POST /telemetry/incidents/{id}/apply   — apply the suggestion's changes.
  Only whitelisted fields with validated ranges ever reach SQL; the LLM
  proposes, this code disposes.
- POST /telemetry/incidents/{id}/dismiss — operator says "benign"; the
  watcher won't reopen the same (camera, kind) for its cooldown window.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from baba_core.light_profiles import apply_settings, capture_profile, record_change, snapshot_live
from fastapi import APIRouter, Depends, HTTPException, Request

from baba_api.audit import write_audit
from baba_api.auth import AuthUser, current_user
from baba_api.incident_replay import (
    REPLAY_SPAN_S,
    build_comparison,
    fetch_trace,
    resolve_rules,
)
from baba_api.models import (
    IncidentAnalyzeOut,
    IncidentChange,
    IncidentOut,
    TelemetrySettings,
    TelemetryUsage,
    adaptive_rate_error,
)
from baba_api.routes_ai import (
    _downscale_for_vlm,
    _fetch_snapshot,
    _json_from_vlm,
    _vision_json,
    pick_active_provider,
)

log = logging.getLogger(__name__)

telemetry_router = APIRouter(tags=["telemetry"])

# What the AI is allowed to touch, with hard ranges (mirrors the pydantic
# constraints on the manual endpoints). Everything else in a suggestion is
# advice for the human, not an actionable change.
_CAMERA_RULE_FIELDS: dict[str, tuple[float, float]] = {
    "min_confidence": (0.0, 1.0),
    "min_box_pct": (0.0, 100.0),
}
_CAMERA_FIELDS: dict[str, tuple[float, float]] = {
    "stillness_ratio": (0.01, 1.0),
    "park_seconds": (5, 3600),
    "idle_fps": (1, 30),
    "target_fps": (1, 30),
}
_INT_CAMERA_FIELDS = {"park_seconds", "idle_fps", "target_fps"}


def _pool(request: Request):
    return request.app.state.pool


def _row_to_out(row) -> IncidentOut:
    def _jsonb(v) -> dict[str, Any] | None:
        if v is None:
            return None
        return json.loads(v) if isinstance(v, str) else v

    return IncidentOut(
        id=row["id"],
        camera_slug=row["camera_slug"],
        kind=row["kind"],
        opened_at=row["opened_at"],
        closed_at=row["closed_at"],
        status=row["status"],
        details=_jsonb(row["details"]) or {},
        verdict=row["verdict"],
        suggestion=_jsonb(row["suggestion"]),
    )


_INCIDENT_COLS = "id, camera_slug, kind, opened_at, closed_at, status, details, verdict, suggestion"

AUTO_ANALYZE_KEY = "telemetry_auto_analyze"


async def auto_analyze_enabled(pool) -> bool:
    raw = await pool.fetchval("SELECT value FROM app_settings WHERE key = $1", AUTO_ANALYZE_KEY)
    if raw is None:
        return False
    return bool(json.loads(raw) if isinstance(raw, str) else raw)


@telemetry_router.get("/telemetry/settings", response_model=TelemetrySettings)
async def get_telemetry_settings(request: Request) -> TelemetrySettings:
    return TelemetrySettings(auto_analyze=await auto_analyze_enabled(_pool(request)))


@telemetry_router.get("/telemetry/usage", response_model=TelemetryUsage)
async def get_telemetry_usage(request: Request) -> TelemetryUsage:
    """Cumulative AI spend of incident analyses (manual + auto), summed
    from the per-incident usage recorded in `suggestion`. Covers only this
    feature — zone suggestions etc. are not counted here."""
    row = await _pool(request).fetchrow(
        """
        SELECT count(*) AS analyses,
               COALESCE(SUM((suggestion->'usage'->>'input_tokens')::bigint), 0) AS input_tokens,
               COALESCE(SUM((suggestion->'usage'->>'output_tokens')::bigint), 0) AS output_tokens
        FROM telemetry_incidents
        WHERE suggestion ? 'usage'
        """
    )
    return TelemetryUsage(
        analyses=row["analyses"],
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
    )


@telemetry_router.put("/telemetry/settings", response_model=TelemetrySettings)
async def put_telemetry_settings(
    payload: TelemetrySettings,
    request: Request,
    user: AuthUser = Depends(current_user),
) -> TelemetrySettings:
    await _pool(request).execute(
        """
        INSERT INTO app_settings (key, value) VALUES ($1, $2::jsonb)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
        """,
        AUTO_ANALYZE_KEY,
        json.dumps(payload.auto_analyze),
    )
    await write_audit(
        _pool(request),
        user=user,
        resource_type="telemetry_settings",
        op="update",
        resource_id=None,
        payload={"auto_analyze": payload.auto_analyze},
    )
    return payload


@telemetry_router.get("/telemetry/incidents", response_model=list[IncidentOut])
async def list_incidents(
    request: Request,
    include_closed: bool = True,
    limit: int = 50,
) -> list[IncidentOut]:
    limit = max(1, min(limit, 200))
    where = "" if include_closed else "WHERE status IN ('open', 'analyzed')"
    rows = await _pool(request).fetch(
        f"""
        SELECT {_INCIDENT_COLS}
        FROM telemetry_incidents
        {where}
        ORDER BY (status IN ('open', 'analyzed')) DESC, opened_at DESC
        LIMIT $1
        """,  # noqa: S608
        limit,
    )
    return [_row_to_out(r) for r in rows]


# --- AI analyze -------------------------------------------------------------

_ANALYZE_SYSTEM = (
    "You are the diagnostics assistant inside BABA, a self-hosted NVR. "
    "Pipeline: per-camera RTSP decode → transformer object detector (COCO "
    "classes) → per-camera tracker (Norfair + class-vote stabilizer + motion "
    "state active/stationary/parked) → events/recording. Cameras with "
    "idle_fps configured run ADAPTIVE rate: full target_fps while the "
    "tracker reports activity, decimated to idle_fps when the scene is "
    "quiet — so false positives or box jitter directly waste GPU.\n"
    "A deterministic watcher opened the incident you are given. Kinds:\n"
    "- active_pinned: camera stuck at full rate >80% of the window with "
    "zero ACTIVE tracks — something (FP flicker, geometry jitter) holds the "
    "rate up without real activity.\n"
    "- drop_spike: detection rules are discarding many times more "
    "detections than get published — a threshold may be blinding a class "
    "the operator wants, OR correctly suppressing an FP cluster. This counts "
    "below-MAINTAIN and size drops only (see two-threshold below).\n"
    "You receive: incident evidence, a 30-minute telemetry summary "
    "(published detections per class with confidence/size quantiles, drop "
    "counters by reason, track births/flips/motion states, adaptive duty "
    "cycle), the camera's current rules and settings, and a live snapshot "
    "of the scene. Sizes are max(bbox_w/frame_w, bbox_h/frame_h) in % — "
    "the same metric as the min_box_pct rule. scene_light is MEASURED from "
    "the decoded frames: luma_avg 0-255 (brightness), ir_ratio 0-1 "
    "(fraction of the window the camera spent in monochrome IR night "
    "mode). Weigh it — night-IR scenes produce different false-positive "
    "patterns than daylight, and a threshold tuned for one may be wrong "
    "for the other.\n"
    "TWO-THRESHOLD TRACKING — read this before reasoning about confidence. "
    "min_confidence is NOT a publish gate any more, it is the BIRTH gate: "
    "only a detection at/above it may START a track. The detector also "
    "publishes everything down to a low MAINTAIN floor (~0.30); those "
    "below-birth detections are 'maintain-only' — they KEEP an existing "
    "track alive through a score dip (a person who sits, a car that parks) "
    "but can never spawn one. Consequences you must respect:\n"
    "- `published` totals and the conf quantiles include maintain-only "
    "boxes, so a low conf_p50 does NOT by itself mean noise. Read "
    "`birth_total` — how many of `total` could actually start a track.\n"
    "- RAISING min_confidence does NOT remove those low-confidence boxes "
    "(they still publish down to the maintain floor); it only makes objects "
    "HARDER to detect in the first place. Do not propose raising it to "
    "'clean up' a low conf distribution — that will not do what you expect.\n"
    "- `dropped.conf` counts only detections below the MAINTAIN floor, so a "
    "small conf-drop count no longer proves the threshold is fine.\n"
    "- A class with high `total` and `birth_total` near 0 is BLIND: lower "
    "min_confidence. A class with healthy births and a long maintain-only "
    "tail is HEALTHY — that tail is the feature working.\n"
    "Tunable knobs (ranges enforced server-side):\n"
    "- camera_rule per class: min_confidence [0-1] (BIRTH gate), "
    "min_box_pct [0-100]\n"
    "- camera: stillness_ratio [0.01-1] (object-relative stillness radius; "
    "higher tolerates more box jitter before a parked object counts as "
    "moving), park_seconds [5-3600], idle_fps [1-30], target_fps [1-30]\n"
    "Judge whether the pattern is justified by real activity (visible in "
    "the snapshot / consistent with published person/vehicle detections) "
    "or a configuration/model problem. Only propose a change when the "
    "evidence clearly supports it — an empty changes list with a clear "
    "verdict is a good answer. Never propose disabling the person class "
    "or raising person min_confidence above 0.55.\n"
    "Output ONLY a JSON object, no prose, no fences:\n"
    '{"verdict": str, "root_cause": one of ["real_activity",'
    '"false_positives","misconfigured_rule","model_instability",'
    '"scene_change","unknown"], "changes": [{"target": "camera_rule"|'
    '"camera", "class_name": str|null, "field": str, "value": number, '
    '"reason": str}]}'
)


def _summarize_buckets(rows) -> dict[str, Any]:
    """Fold 30 minutes of per-minute buckets into one compact dict the
    model can actually read — totals and representative quantiles instead
    of 90 raw payloads."""
    det_pub: dict[str, dict[str, Any]] = {}
    det_drop: dict[str, dict[str, int]] = {}
    tracker = {"births": 0, "flips": 0, "max_active": 0, "max_stationary": 0, "max_parked": 0}
    ingest = {"active_s": 0.0, "idle_s": 0.0, "transitions": 0, "frames_out": 0, "frames_dropped_idle": 0}
    luma: list[float] = []
    ir: list[float] = []
    n_minutes = 0
    for r in rows:
        p = json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"]
        if r["source"] == "detector":
            n_minutes += 1
            for cls, st in p.get("published", {}).items():
                agg = det_pub.setdefault(
                    cls,
                    {
                        "n": 0,
                        "n_birth": 0,
                        "conf_p50": [],
                        "conf_p95": [],
                        "size_p50": [],
                        "size_p95": [],
                    },
                )
                agg["n"] += st.get("n", 0)
                agg["n_birth"] += st.get("n_birth", 0)
                for k in ("conf_p50", "conf_p95", "size_p50", "size_p95"):
                    agg[k].append(st.get(k, 0.0))
            for cls, reasons in p.get("dropped", {}).items():
                d = det_drop.setdefault(cls, {})
                for reason, n in reasons.items():
                    if n:
                        d[reason] = d.get(reason, 0) + n
        elif r["source"] == "tracker":
            tracker["births"] += p.get("births", 0)
            tracker["flips"] += p.get("flips", 0)
            tracker["max_active"] = max(tracker["max_active"], p.get("n_active", 0))
            tracker["max_stationary"] = max(tracker["max_stationary"], p.get("n_stationary", 0))
            tracker["max_parked"] = max(tracker["max_parked"], p.get("n_parked", 0))
        elif r["source"] == "ingestor":
            for k in ingest:
                ingest[k] += p.get(k, 0)
            if p.get("luma_avg", -1.0) >= 0:
                luma.append(p["luma_avg"])
            if p.get("ir_ratio", -1.0) >= 0:
                ir.append(p["ir_ratio"])

    published = {
        cls: {
            "total": agg["n"],
            "per_min": round(agg["n"] / max(n_minutes, 1), 1),
            # Of `total`, how many cleared the BIRTH threshold (could start a
            # track). total - birth_total are maintain-only: they hold an
            # existing track through a score dip but never spawn one. The conf
            # quantiles below span BOTH bands, so read them together with this.
            "birth_total": agg["n_birth"],
            **{
                k: round(sum(v) / len(v), 3) if (v := agg[k]) else 0.0
                for k in ("conf_p50", "conf_p95", "size_p50", "size_p95")
            },
        }
        for cls, agg in det_pub.items()
    }
    coverage = ingest["active_s"] + ingest["idle_s"]
    return {
        "window_minutes": n_minutes,
        "detector": {"published": published, "dropped": det_drop},
        "tracker": tracker,
        "adaptive_rate": {
            **{k: round(v, 1) if isinstance(v, float) else v for k, v in ingest.items()},
            "duty_active": round(ingest["active_s"] / coverage, 3) if coverage else None,
        },
        # Measured from the frames (not the camera's configured mode):
        # brightness trend + whether the camera sat in IR night mode, plus
        # the latest bucket so a mid-window day/night flip is visible.
        "scene_light": {
            "luma_avg": round(sum(luma) / len(luma)) if luma else None,
            "luma_latest": round(luma[-1]) if luma else None,
            "ir_ratio": round(sum(ir) / len(ir), 2) if ir else None,
            "ir_latest": bool(ir[-1] > 0.5) if ir else None,
        },
    }


async def _build_context(pool, incident) -> dict[str, Any]:
    slug = incident["camera_slug"]
    cam = await pool.fetchrow(
        """
        SELECT id, slug, name, target_fps, idle_fps, stillness_ratio, park_seconds,
               light_condition
        FROM cameras WHERE slug = $1
        """,
        slug,
    )
    if cam is None:
        raise HTTPException(404, f"camera {slug!r} not found")
    global_rules = await pool.fetch(
        "SELECT class_name, min_confidence, enabled, min_box_pct FROM detector_global_rules"
    )
    camera_rules = await pool.fetch(
        "SELECT class_name, min_confidence, enabled, min_box_pct "
        "FROM camera_detection_rules WHERE camera_id = $1",
        cam["id"],
    )
    buckets = await pool.fetch(
        """
        SELECT source, payload FROM camera_telemetry
        WHERE camera_slug = $1 AND at > now() - interval '30 minutes'
        ORDER BY at
        """,
        slug,
    )

    def _rules(rows):
        return {
            r["class_name"]: {
                "min_confidence": float(r["min_confidence"]) if r["min_confidence"] is not None else None,
                "enabled": r["enabled"],
                "min_box_pct": float(r["min_box_pct"]) if r["min_box_pct"] is not None else None,
            }
            for r in rows
        }

    # Camera tracking values are layered since migration 053 — a NULL column
    # inherits the global tracking_defaults. The AI reasons about EFFECTIVE
    # values; a suggested camera change then simply writes an override.
    from baba_api.routes import _TRACKING_DEFAULTS_KEY, _TRACKING_FALLBACK

    g_raw = await pool.fetchval(
        "SELECT value FROM app_settings WHERE key = $1", _TRACKING_DEFAULTS_KEY
    )
    if isinstance(g_raw, str):
        g_raw = json.loads(g_raw)
    tracking_globals = {**_TRACKING_FALLBACK, **(g_raw or {})}

    details = incident["details"]
    return {
        "incident": {
            "kind": incident["kind"],
            "opened_at": incident["opened_at"].isoformat(),
            "evidence": json.loads(details) if isinstance(details, str) else details,
        },
        "camera": {
            "slug": cam["slug"],
            "name": cam["name"],
            "target_fps": cam["target_fps"],
            "idle_fps": cam["idle_fps"],
            "stillness_ratio": (
                float(cam["stillness_ratio"])
                if cam["stillness_ratio"] is not None
                else float(tracking_globals["stillness_ratio"])
            ),
            "park_seconds": (
                cam["park_seconds"]
                if cam["park_seconds"] is not None
                else int(tracking_globals["park_seconds"])
            ),
            "light_condition": cam["light_condition"],
        },
        "rules": {"global": _rules(global_rules), "camera_overrides": _rules(camera_rules)},
        "telemetry_last_30min": _summarize_buckets(buckets),
    }


def _parse_analysis(text: str) -> tuple[str, str, list[IncidentChange]] | None:
    data = _json_from_vlm(text)
    if not isinstance(data, dict) or not isinstance(data.get("verdict"), str):
        return None
    root_cause = data.get("root_cause")
    if root_cause not in (
        "real_activity",
        "false_positives",
        "misconfigured_rule",
        "model_instability",
        "scene_change",
        "unknown",
    ):
        root_cause = "unknown"
    changes: list[IncidentChange] = []
    raw = data.get("changes")
    if isinstance(raw, list):
        for c in raw:
            if not isinstance(c, dict):
                continue
            try:
                change = IncidentChange(**c)
                _validate_change(change)
            except (ValueError, TypeError) as e:
                log.info("dropping unusable AI change %r: %s", c, e)
                continue
            changes.append(change)
    return data["verdict"].strip(), root_cause, changes


def _validate_change(change: IncidentChange) -> None:
    if change.target == "camera_rule":
        if not change.class_name:
            raise ValueError("camera_rule change needs class_name")
        bounds = _CAMERA_RULE_FIELDS.get(change.field)
    else:
        bounds = _CAMERA_FIELDS.get(change.field)
    if bounds is None:
        raise ValueError(f"field {change.field!r} is not tunable via incidents")
    lo, hi = bounds
    if not (lo <= change.value <= hi):
        raise ValueError(f"{change.field}={change.value} outside [{lo}, {hi}]")
    # Guardrail mirrored from the prompt: the AI must never be able to
    # blind person detection even if it ignores its instructions.
    if (
        change.target == "camera_rule"
        and change.class_name == "person"
        and change.field == "min_confidence"
        and change.value > 0.55
    ):
        raise ValueError("refusing person min_confidence > 0.55")


def _revalidated(raw_changes: list) -> list[IncidentChange]:
    """Re-validate before acting: the DB row is operator-visible but not
    operator-authored, and ranges may have tightened since analyze."""
    try:
        changes = [IncidentChange(**c) for c in raw_changes]
        for change in changes:
            _validate_change(change)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, f"suggested change rejected: {e}") from e
    return changes


async def run_incident_analysis(
    pool,
    *,
    secret_key: str,
    go2rtc_url: str,
    go2rtc_auth: tuple[str, str],
    incident,
    provider: str | None = None,
) -> IncidentAnalyzeOut:
    """Request-free analysis core, shared by the manual route and the
    auto-analyzer background task."""
    active_provider, api_key, model = await pick_active_provider(pool, secret_key, provider)
    context = await _build_context(pool, incident)

    snapshot = await _fetch_snapshot(go2rtc_url, incident["camera_slug"], go2rtc_auth)
    snapshot = await asyncio.to_thread(_downscale_for_vlm, snapshot)

    result = await _vision_json(
        active_provider,
        api_key,
        model,
        system=_ANALYZE_SYSTEM,
        user_text=(
            "Analyze this incident. Write `verdict` and every change `reason` "
            "in Croatian (professional register). Context:\n"
            + json.dumps(context, ensure_ascii=False)
        ),
        image_jpeg=snapshot,
    )
    if not result.ok:
        return IncidentAnalyzeOut(model=model, latency_ms=result.latency_ms, error=result.error)

    parsed = _parse_analysis(result.text or "")
    if parsed is None:
        log.info("incident analyze: unparseable response %r", (result.text or "")[:400])
        return IncidentAnalyzeOut(
            model=result.model or model,
            latency_ms=result.latency_ms,
            error="model returned no parseable verdict",
        )
    verdict, root_cause, changes = parsed

    suggestion = {
        "root_cause": root_cause,
        "changes": [c.model_dump() for c in changes],
        "model": result.model or model,
        "latency_ms": result.latency_ms,
        "usage": {
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
        },
    }
    row = await pool.fetchrow(
        f"""
        UPDATE telemetry_incidents
        SET verdict = $2, suggestion = $3::jsonb,
            status = CASE WHEN status IN ('open', 'analyzed') THEN 'analyzed' ELSE status END
        WHERE id = $1
        RETURNING {_INCIDENT_COLS}
        """,  # noqa: S608
        incident["id"],
        verdict,
        json.dumps(suggestion),
    )
    await pool.execute(
        "UPDATE ai_settings SET last_used_at = now() WHERE provider = $1", active_provider
    )
    return IncidentAnalyzeOut(
        model=result.model or model,
        latency_ms=result.latency_ms,
        incident=_row_to_out(row),
    )


@telemetry_router.post("/telemetry/incidents/{incident_id}/analyze", response_model=IncidentAnalyzeOut)
async def analyze_incident(
    incident_id: UUID,
    request: Request,
    provider: str | None = None,
) -> IncidentAnalyzeOut:
    pool = _pool(request)
    incident = await pool.fetchrow(
        f"SELECT {_INCIDENT_COLS} FROM telemetry_incidents WHERE id = $1", incident_id  # noqa: S608
    )
    if incident is None:
        raise HTTPException(404, "incident not found")
    cfg = request.app.state.config
    return await run_incident_analysis(
        pool,
        secret_key=request.app.state.secret_key,
        go2rtc_url=cfg.go2rtc_url,
        go2rtc_auth=cfg.go2rtc_auth,
        incident=incident,
        provider=provider,
    )


# --- replay: what the suggestion WOULD have done ----------------------------


@telemetry_router.post("/telemetry/incidents/{incident_id}/replay")
async def replay_incident(
    incident_id: UUID,
    request: Request,
    user: AuthUser = Depends(current_user),
) -> dict[str, Any]:
    """Side-by-side evidence for the suggested change, over the footage the
    incident was opened on.

    Returns the clip window (cut by the existing recorder endpoint) plus, per
    frame, the boxes the CURRENT thresholds admit and the boxes the PROPOSED
    ones would — from the detector's live pre-gate trace, through the live
    gate. Nothing is changed; this only shows.

    The result is stored on the incident. The detector's trace ring is a flight
    recorder that rolls, so an incident reviewed hours later would otherwise
    have no evidence left — computing once and keeping it is what makes the
    decision available when the operator actually gets to it.
    """
    pool = _pool(request)
    incident = await pool.fetchrow(
        f"SELECT {_INCIDENT_COLS} FROM telemetry_incidents WHERE id = $1", incident_id  # noqa: S608
    )
    if incident is None:
        raise HTTPException(404, "incident not found")

    details = incident["details"]
    details = json.loads(details) if isinstance(details, str) else (details or {})
    cached = details.get("replay")
    if cached:
        return cached

    suggestion = incident["suggestion"]
    suggestion = json.loads(suggestion) if isinstance(suggestion, str) else suggestion
    raw_changes = (suggestion or {}).get("changes") or []
    if not raw_changes:
        raise HTTPException(400, "no suggested changes — run analyze first")
    changes = _revalidated(raw_changes)

    cam = await pool.fetchrow(
        "SELECT id, slug FROM cameras WHERE slug = $1", incident["camera_slug"]
    )
    if cam is None:
        raise HTTPException(404, f"camera {incident['camera_slug']!r} not found")

    # The watcher aggregates a 15-minute window and opens the incident at the
    # end of it, so the behaviour that tripped it sits just BEFORE opened_at.
    end_s = incident["opened_at"].timestamp()
    start_s = end_s - REPLAY_SPAN_S

    nc = getattr(request.app.state, "nats", None)
    if nc is None:
        raise HTTPException(503, "detector control bus unavailable")
    try:
        trace = await fetch_trace(
            nc, cam["slug"], int(start_s * 1_000_000_000), int(end_s * 1_000_000_000)
        )
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from None

    if not trace.get("frames"):
        coverage = trace.get("coverage")
        if coverage is None:
            detail = "the detector holds no trace for this camera (restarted since?)"
        else:
            oldest = coverage["oldest_ns"] / 1_000_000_000
            detail = (
                "the detector's trace no longer reaches this incident — it retains "
                f"from {datetime.fromtimestamp(oldest, tz=UTC):%Y-%m-%d %H:%M:%S}Z onward"
            )
        raise HTTPException(409, detail)

    rules, maintain_conf = await resolve_rules(pool, cam["id"], changes)
    comparison = build_comparison(trace, rules, maintain_conf)

    # Tracker-level knobs (park_seconds, stillness_ratio) cannot be judged from
    # boxes on a frame — they play out over a track's lifetime. Say so plainly
    # rather than letting a picture imply it covered them.
    not_shown = sorted({c.field for c in changes if c.target != "camera_rule"})
    payload = {
        "incident_id": str(incident_id),
        "camera_id": str(cam["id"]),
        "camera_slug": cam["slug"],
        "start_s": start_s,
        "end_s": end_s,
        "changes": [c.model_dump() for c in changes],
        "not_shown": not_shown,
        **comparison,
    }
    await pool.execute(
        "UPDATE telemetry_incidents SET details = details || jsonb_build_object('replay', $2::jsonb) "
        "WHERE id = $1",
        incident_id,
        json.dumps(payload),
    )
    return payload


# --- apply / dismiss --------------------------------------------------------


@telemetry_router.post("/telemetry/incidents/{incident_id}/apply", response_model=IncidentOut)
async def apply_incident(
    incident_id: UUID,
    request: Request,
    user: AuthUser = Depends(current_user),
) -> IncidentOut:
    pool = _pool(request)
    incident = await pool.fetchrow(
        f"SELECT {_INCIDENT_COLS} FROM telemetry_incidents WHERE id = $1", incident_id  # noqa: S608
    )
    if incident is None:
        raise HTTPException(404, "incident not found")
    if incident["status"] not in ("open", "analyzed"):
        raise HTTPException(409, f"incident is {incident['status']}, nothing to apply")

    suggestion = incident["suggestion"]
    suggestion = json.loads(suggestion) if isinstance(suggestion, str) else suggestion
    raw_changes = (suggestion or {}).get("changes") or []
    if not raw_changes:
        raise HTTPException(400, "no suggested changes — run analyze first")

    cam = await pool.fetchrow(
        "SELECT id, target_fps, idle_fps, park_seconds, light_condition "
        "FROM cameras WHERE slug = $1",
        incident["camera_slug"],
    )
    if cam is None:
        raise HTTPException(404, f"camera {incident['camera_slug']!r} not found")

    changes = _revalidated(raw_changes)

    target_fps = cam["target_fps"]
    idle_fps = cam["idle_fps"]
    for change in changes:
        if change.target == "camera" and change.field == "target_fps":
            target_fps = int(change.value)
        if change.target == "camera" and change.field == "idle_fps":
            idle_fps = int(change.value)
    err = adaptive_rate_error(idle_fps, target_fps)
    if err:
        raise HTTPException(400, f"suggested change rejected: {err}")

    async with pool.acquire() as conn, conn.transaction():
        # "Before" side of the journal row the one-click revert reads.
        before_snap = await snapshot_live(conn, cam["id"])
        before_snap["camera"] = {
            "target_fps": cam["target_fps"],
            "idle_fps": cam["idle_fps"],
            "park_seconds": cam["park_seconds"],
        }
        for change in changes:
            if change.target == "camera_rule":
                # Field name comes from the _CAMERA_RULE_FIELDS whitelist —
                # validated above, safe to interpolate.
                await conn.execute(
                    f"""
                    INSERT INTO camera_detection_rules (camera_id, class_name, {change.field})
                    VALUES ($1, $2, $3)
                    ON CONFLICT (camera_id, class_name)
                    DO UPDATE SET {change.field} = EXCLUDED.{change.field}
                    """,  # noqa: S608
                    cam["id"],
                    change.class_name,
                    change.value,
                )
            else:
                value: float | int = (
                    int(change.value) if change.field in _INT_CAMERA_FIELDS else change.value
                )
                await conn.execute(
                    f"UPDATE cameras SET {change.field} = $2 WHERE id = $1",  # noqa: S608
                    cam["id"],
                    value,
                )
        after_snap = await snapshot_live(conn, cam["id"])
        after_cam = await conn.fetchrow(
            "SELECT target_fps, idle_fps, park_seconds FROM cameras WHERE id = $1", cam["id"]
        )
        after_snap["camera"] = dict(after_cam)
        # The applied settings become the validated profile for the band
        # this incident happened under — the correlation table entry.
        await record_change(
            conn,
            cam["id"],
            source="incident_apply",
            incident_id=incident_id,
            light_condition=cam["light_condition"],
            before=before_snap,
            after=after_snap,
        )
        await capture_profile(conn, cam["id"], cam["light_condition"])
        row = await conn.fetchrow(
            f"""
            UPDATE telemetry_incidents
            SET status = 'applied', closed_at = now()
            WHERE id = $1
            RETURNING {_INCIDENT_COLS}
            """,  # noqa: S608
            incident_id,
        )
    await write_audit(
        pool,
        user=user,
        resource_type="telemetry_incident",
        op="apply",
        resource_id=incident_id,
        payload={
            "camera_slug": incident["camera_slug"],
            "kind": incident["kind"],
            "changes": [c.model_dump() for c in changes],
        },
    )
    return _row_to_out(row)


@telemetry_router.post("/telemetry/incidents/{incident_id}/revert", response_model=IncidentOut)
async def revert_incident(
    incident_id: UUID,
    request: Request,
    user: AuthUser = Depends(current_user),
) -> IncidentOut:
    """One-click undo of an applied incident: restore the journaled
    `before` snapshot, stamp it as the profile for the camera's CURRENT
    band, and journal the revert itself."""
    pool = _pool(request)
    incident = await pool.fetchrow(
        f"SELECT {_INCIDENT_COLS} FROM telemetry_incidents WHERE id = $1", incident_id  # noqa: S608
    )
    if incident is None:
        raise HTTPException(404, "incident not found")
    if incident["status"] != "applied":
        raise HTTPException(409, f"incident is {incident['status']}, nothing to revert")

    change = await pool.fetchrow(
        """
        SELECT before FROM camera_setting_changes
        WHERE incident_id = $1 AND source = 'incident_apply'
        ORDER BY created_at DESC LIMIT 1
        """,
        incident_id,
    )
    if change is None:
        raise HTTPException(409, "no journaled change for this incident (applied pre-journal)")
    saved_before = change["before"]
    saved_before = json.loads(saved_before) if isinstance(saved_before, str) else saved_before

    cam = await pool.fetchrow(
        "SELECT id, light_condition FROM cameras WHERE slug = $1", incident["camera_slug"]
    )
    if cam is None:
        raise HTTPException(404, f"camera {incident['camera_slug']!r} not found")

    async with pool.acquire() as conn, conn.transaction():
        before_now = await snapshot_live(conn, cam["id"])
        await apply_settings(conn, cam["id"], saved_before)
        cam_fields = saved_before.get("camera") or {}
        if cam_fields:
            await conn.execute(
                "UPDATE cameras SET target_fps = $2, idle_fps = $3, park_seconds = $4 "
                "WHERE id = $1",
                cam["id"],
                cam_fields.get("target_fps"),
                cam_fields.get("idle_fps"),
                cam_fields.get("park_seconds"),
            )
        await record_change(
            conn,
            cam["id"],
            source="revert",
            incident_id=incident_id,
            light_condition=cam["light_condition"],
            before=before_now,
            after=saved_before,
        )
        await capture_profile(conn, cam["id"], cam["light_condition"])
        row = await conn.fetchrow(
            f"""
            UPDATE telemetry_incidents
            SET status = 'reverted'
            WHERE id = $1
            RETURNING {_INCIDENT_COLS}
            """,  # noqa: S608
            incident_id,
        )
    await write_audit(
        pool,
        user=user,
        resource_type="telemetry_incident",
        op="revert",
        resource_id=incident_id,
        payload={"camera_slug": incident["camera_slug"], "kind": incident["kind"]},
    )
    return _row_to_out(row)


@telemetry_router.post("/telemetry/incidents/{incident_id}/dismiss", response_model=IncidentOut)
async def dismiss_incident(
    incident_id: UUID,
    request: Request,
    user: AuthUser = Depends(current_user),
) -> IncidentOut:
    pool = _pool(request)
    row = await pool.fetchrow(
        f"""
        UPDATE telemetry_incidents
        SET status = 'dismissed', closed_at = now()
        WHERE id = $1 AND status IN ('open', 'analyzed')
        RETURNING {_INCIDENT_COLS}
        """,  # noqa: S608
        incident_id,
    )
    if row is None:
        exists = await pool.fetchval(
            "SELECT status FROM telemetry_incidents WHERE id = $1", incident_id
        )
        if exists is None:
            raise HTTPException(404, "incident not found")
        raise HTTPException(409, f"incident is already {exists}")
    await write_audit(
        pool,
        user=user,
        resource_type="telemetry_incident",
        op="dismiss",
        resource_id=incident_id,
        payload={"camera_slug": row["camera_slug"], "kind": row["kind"]},
    )
    return _row_to_out(row)
