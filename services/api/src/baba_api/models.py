from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


def _validate_points(points: list[list[float]]) -> list[list[float]]:
    """Each point must be exactly [x, y] normalized to [0, 1]. Rejects malformed
    vertices (wrong arity, out-of-range) at parse time (422) instead of letting
    them blow up as a 500 deep inside cv2.fillPoly / the polygon overlay."""
    for pt in points:
        if len(pt) != 2:
            raise ValueError("each point must be [x, y]")
        x, y = pt
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            raise ValueError("point coords must be normalized to [0, 1]")
    return points


_COLOR_PATTERN = r"^#[0-9a-fA-F]{6}$"

_MAX_EDGE_DESC = (
    "Long-edge cap the decoder downscales the stream to before it reaches the "
    "SHM ring. 0 = no downscale, keep the stream at native resolution. "
    "Otherwise >= 320."
)


def _check_max_edge(v: int | None) -> int | None:
    """Mirror the DB CHECK (`= 0 OR >= 320`) exactly.

    A plain `ge=320` was here until 2026-07-15 and silently made `0` — the
    documented "no downscale" value — unreachable through the API and the UI,
    even though migration 019 had already relaxed the constraint at the data
    layer specifically so the "off" value would be expressible. The feature
    existed in the DB and in `scaled_dims()` and was dead everywhere above it.

    It matters: the downscale is applied on the decoder's hardware scaler, so
    the pixels are gone before Python ever sees a frame. On the patio camera
    that turned a 41 px inter-ocular face (above ArcFace's 35.24 px template)
    into 13 px, which is why face recognition never worked there.
    """
    if v is None or v == 0 or v >= 320:
        return v
    raise ValueError("downscale_max_edge must be 0 (native, no downscale) or >= 320")


_SLUG_PATTERN = r"^[a-z0-9][a-z0-9_-]*$"


def adaptive_rate_error(idle_fps: int | None, target_fps: int) -> str | None:
    """The adaptive-rate contract, held in one place because four sites ask
    it: the create model, the PATCH route, the incident-apply route and the
    DB CHECK. idle_fps must leave the ingestor somewhere to decay TO — equal
    rates are adaptive-off written as adaptive-on, and every consumer that
    reads `idle_fps IS NOT NULL` as "this camera decimates" then reads it
    wrong. Measured: patio sat at idle_fps = target_fps = 4 and tripped the
    active_pinned watcher on 30 days out of 30."""
    if idle_fps is not None and idle_fps >= target_fps:
        return "idle_fps must be below target_fps"
    return None


AnalysisStream = Literal["main", "sub"]


def analysis_stream_error(analysis_stream: str, substream_url: str | None) -> str | None:
    """Mirrors the DB CHECK (cameras_analysis_stream) so the create model and
    the PATCH route answer 422, not 500."""
    if analysis_stream == "sub" and not substream_url:
        return "analysis_stream 'sub' needs a substream_url"
    return None


class CameraIn(BaseModel):
    """Payload for POST /cameras. slug is required; everything else has a default."""

    slug: str = Field(..., min_length=1, max_length=64, pattern=_SLUG_PATTERN)
    name: str = Field(..., min_length=1, max_length=128)
    stream_url: str = Field(..., min_length=1)
    substream_url: str | None = Field(None, min_length=1)
    analysis_stream: AnalysisStream = "main"
    enabled: bool = True
    target_fps: int = Field(5, ge=1, le=30)
    # Adaptive detection rate: frame rate while the scene is quiet (only
    # parked/no objects). The ingestor snaps back to target_fps on the first
    # fresh detection. None = adaptive off, constant target_fps.
    idle_fps: int | None = Field(None, ge=1, le=30)
    # Tracking tuning (Settings → Cameras → Praćenje) — all four are LAYERED
    # since migration 053: None inherits the global `tracking_defaults` row
    # (Settings → Detection); a value is a per-camera override. stillness =
    # object-relative wobble radius while still counting as motionless;
    # park = stillness time before the parked promotion; lost = how long a
    # track coasts without a matching detection; reid = how long a dead
    # track stays appearance-resurrectable with the SAME id.
    stillness_ratio: float | None = Field(None, ge=0.01, le=1.0)
    park_seconds: int | None = Field(None, ge=5, le=3600)
    lost_seconds: int | None = Field(None, ge=1, le=300)
    reid_lost_seconds: int | None = Field(None, ge=0, le=900)
    # Two-threshold MAINTAIN floor (per-camera, NOT layered — no global
    # fallback). None on create → DB default 0.30.
    maintain_conf: float | None = Field(None, ge=0.0, le=1.0)
    downscale_max_edge: int = Field(1280, description=_MAX_EDGE_DESC)
    recording_enabled: bool = True
    # None = let the server auto-assign the next unused palette colour so every
    # camera is visually distinct in the storage bar / legends. An explicit
    # value is honoured as-is.
    color: str | None = Field(None, pattern=_COLOR_PATTERN)

    _v_max_edge = field_validator("downscale_max_edge")(_check_max_edge)

    @model_validator(mode="after")
    def _idle_below_target(self) -> CameraIn:
        err = adaptive_rate_error(self.idle_fps, self.target_fps) or analysis_stream_error(
            self.analysis_stream, self.substream_url
        )
        if err:
            raise ValueError(err)
        return self


class CameraPatch(BaseModel):
    """Partial update. Only fields that are sent get updated. Slug is
    intentionally not patchable here — renaming it would orphan media
    paths, NATS subjects, and shm rings, so a dedicated endpoint
    handles that with a coordinated filesystem rename."""

    name: str | None = Field(None, min_length=1, max_length=128)
    stream_url: str | None = Field(None, min_length=1)
    substream_url: str | None = Field(None, min_length=1)
    analysis_stream: AnalysisStream | None = None
    enabled: bool | None = None
    target_fps: int | None = Field(None, ge=1, le=30)
    # Explicit null clears idle_fps (adaptive off); cross-field consistency
    # against the effective target_fps is enforced in the PATCH route where
    # the current row is known.
    idle_fps: int | None = Field(None, ge=1, le=30)
    # Explicit null on any of the four tracking fields clears the per-camera
    # override → the camera inherits the global tracking_defaults again.
    stillness_ratio: float | None = Field(None, ge=0.01, le=1.0)
    park_seconds: int | None = Field(None, ge=5, le=3600)
    lost_seconds: int | None = Field(None, ge=1, le=300)
    reid_lost_seconds: int | None = Field(None, ge=0, le=900)
    # None = leave unchanged (per-camera, always set — never clears to a global).
    maintain_conf: float | None = Field(None, ge=0.0, le=1.0)
    downscale_max_edge: int | None = Field(None, description=_MAX_EDGE_DESC)
    recording_enabled: bool | None = None
    color: str | None = Field(None, pattern=_COLOR_PATTERN)

    _v_max_edge = field_validator("downscale_max_edge")(_check_max_edge)


class LiveOverlay(BaseModel):
    """Global live-view overlay toggles (app_settings.live_overlay). `boxes`
    draws detection rectangles on the grid stills + the detail-view MSE canvas;
    `badges` shows per-tile detection-count chips on the grid."""

    boxes: bool = True
    badges: bool = True


class TrackingDefaults(BaseModel):
    """Global tracking defaults (app_settings.tracking_defaults) — the base
    layer every camera inherits unless it carries a per-camera override."""

    stillness_ratio: float = Field(..., ge=0.01, le=1.0)
    park_seconds: int = Field(..., ge=5, le=3600)
    lost_seconds: int = Field(..., ge=1, le=300)
    reid_lost_seconds: int = Field(..., ge=0, le=900)


class CameraSlugRename(BaseModel):
    """Dedicated payload for renaming a camera's slug. Validated
    against the same pattern as CameraIn.slug. The endpoint performs
    a coordinated rename of the DB row, the media directory, and any
    recordings.rel_path entries that reference the old slug."""

    slug: str = Field(..., min_length=1, max_length=64, pattern=_SLUG_PATTERN)


class Camera(BaseModel):
    id: UUID
    slug: str
    name: str
    stream_url: str
    substream_url: str | None
    analysis_stream: AnalysisStream
    enabled: bool
    target_fps: int
    idle_fps: int | None
    # None = inherits the global tracking_defaults (Settings → Detection).
    stillness_ratio: float | None
    park_seconds: int | None
    lost_seconds: int | None
    reid_lost_seconds: int | None
    maintain_conf: float
    # Measured illumination band (event-manager writes it from frame
    # telemetry): ir / dark / dim / normal / bright.
    light_condition: str
    downscale_max_edge: int
    recording_enabled: bool
    color: str
    created_at: datetime
    updated_at: datetime


class TestConnectionIn(BaseModel):
    stream_url: str = Field(..., min_length=1)


class TestConnectionOut(BaseModel):
    ok: bool
    codec: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    duration_ms: int
    error: str | None = None


# --- AI settings ----------------------------------------------------------
#
# One row per provider in the DB; the UI today only edits the "anthropic"
# slot but the schema is provider-keyed so adding OpenAI / local llama is
# additive.

# Per-provider defaults. The frontend lets the user override the model.
ANTHROPIC_DEFAULT_MODEL = "claude-opus-4-8"
OPENAI_DEFAULT_MODEL = "gpt-5"
SUPPORTED_AI_PROVIDERS = ("anthropic", "openai")


class AiSettingsIn(BaseModel):
    """Upsert payload. `api_key` is required on create; on update, omit it to
    keep the existing key (so users can edit `model`/`enabled` without
    re-pasting). Empty string is rejected explicitly to avoid accidental wipes."""

    provider: str = Field(..., min_length=1, max_length=32)
    model: str = Field(..., min_length=1, max_length=128)
    api_key: str | None = Field(None, min_length=10, max_length=512)
    enabled: bool = True


class AiSettingsOut(BaseModel):
    provider: str
    model: str
    api_key_masked: str
    enabled: bool
    last_used_at: datetime | None
    created_at: datetime
    updated_at: datetime


class AiTestIn(BaseModel):
    """Optional one-shot test: pass an api_key + model to validate without
    storing. If api_key is omitted, the server uses the stored key."""

    provider: str = Field("anthropic", min_length=1, max_length=32)
    api_key: str | None = Field(None, min_length=10, max_length=512)
    model: str | None = Field(None, min_length=1, max_length=128)


class AiTestOut(BaseModel):
    ok: bool
    model: str | None = None
    latency_ms: int
    error: str | None = None


# --- Zones ----------------------------------------------------------------
#
# Polygons are stored in normalized coordinates so they survive resolution
# changes on the camera. We do shape validation in the API layer (min 3
# vertices, all values in [0,1]).

# Free-form tag: 'entry','exit','restricted','parking','no_go','interest',
# 'generic','ignore'. Kept as text (migration 008) so new kinds need no
# migration. 'ignore' is the one kind with structural teeth — the tracker
# drops every track whose foot position lands in one, so those pixels never
# reach ReID, embedding, events or the UI.
ZoneKind = str


class ZoneClassRulePayload(BaseModel):
    """Optional per-class refinement under zone rules.enabled_classes.<class>.
    See db/migrations/034_detection_rules.sql — every field is optional; absent
    values inherit camera/global defaults.

    `motion_gate` must be carried even though no UI sets it yet. Pydantic
    ignores unknown keys, so while this model lacked the field a zone GET
    stripped it and — worse — any zone create/edit round-tripped rules through
    model_dump and ERASED it from the JSONB. A field the event-manager reads
    (zones.py, _gating.py) must survive the api, or the only way to set it is
    hand-editing Postgres and the only way to lose it is touching the zone in
    the UI."""

    min_confidence: float | None = Field(None, ge=0.0, le=1.0)
    min_area_pct: float | None = Field(None, ge=0.0, le=1.0)
    min_dwell_ms: int | None = Field(None, ge=0)
    cooldown_s: int | None = Field(None, ge=0)
    motion_gate: Literal["moving_only"] | None = None


class ZoneRules(BaseModel):
    """Per-zone detection refinement (layer 3 of three-layer rules).
    `enabled_classes` is the operator's per-zone allowlist; absent or
    empty means "no allowlist — zone fires for any class that passed
    the global + per-camera filter"."""

    enabled_classes: dict[str, ZoneClassRulePayload] = Field(default_factory=dict)


class ZoneIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    kind: ZoneKind = Field("generic", min_length=1, max_length=32)
    polygon: list[list[float]] = Field(..., min_length=3, max_length=64)
    color: str = Field("#f59e0b", pattern=r"^#[0-9a-fA-F]{6}$")
    enabled: bool = True
    rules: ZoneRules = Field(default_factory=ZoneRules)


class ZonePatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=64)
    kind: ZoneKind | None = Field(None, min_length=1, max_length=32)
    polygon: list[list[float]] | None = Field(None, min_length=3, max_length=64)
    color: str | None = Field(None, pattern=r"^#[0-9a-fA-F]{6}$")
    enabled: bool | None = None
    rules: ZoneRules | None = None


class Zone(BaseModel):
    id: UUID
    camera_id: UUID
    name: str
    kind: ZoneKind
    polygon: list[list[float]]
    color: str
    enabled: bool
    rules: ZoneRules
    created_at: datetime
    updated_at: datetime


# Suggested zone (from AI). Same shape as ZoneIn but with a human-readable
# rationale for the UI ("why this zone?"). The frontend can accept/reject
# each suggestion individually before saving.
class ZoneSuggestion(BaseModel):
    name: str
    kind: ZoneKind
    polygon: list[list[float]]
    color: str = "#f59e0b"
    rationale: str = ""


class SuggestZonesOut(BaseModel):
    model: str
    suggestions: list[ZoneSuggestion]
    latency_ms: int
    error: str | None = None


# --- Auto-tune zone rules (AI) -------------------------------------------
# Operator collects N seconds of live detections, aggregates per-zone
# per-class statistics, and asks the AI to propose min_confidence /
# min_area_pct / allowed_classes per zone. The UI shows a diff and lets
# the operator accept/reject per zone — same flow as SuggestZones but
# operating on rules instead of polygons.


class ZoneClassStats(BaseModel):
    """Histogram-style summary of one class's detections inside one zone
    over the observation window. Confidence and area percentiles tell the
    AI roughly how the scores distribute so it can pick a threshold that
    keeps signal and rejects noise."""

    count: int
    conf_p25: float
    conf_p50: float
    conf_p75: float
    conf_max: float
    area_p50: float  # bbox area / frame area, normalized [0, 1]


class ZoneObservation(BaseModel):
    zone_id: str
    zone_name: str
    zone_kind: str
    # Current rules so the AI can describe the delta in its rationale.
    current_rules: dict[str, Any] | None = None
    class_stats: dict[str, ZoneClassStats] = Field(default_factory=dict)


class TuneZoneRulesIn(BaseModel):
    duration_s: float
    frame_width: int
    frame_height: int
    zones: list[ZoneObservation]


class ProposedClassRule(BaseModel):
    min_confidence: float | None = None
    min_area_pct: float | None = None
    min_dwell_ms: int | None = None
    cooldown_s: int | None = None


class ProposedZoneRules(BaseModel):
    zone_id: str
    zone_name: str
    enabled_classes: dict[str, ProposedClassRule]
    rationale: str = ""


class TuneZoneRulesOut(BaseModel):
    model: str
    proposals: list[ProposedZoneRules]
    latency_ms: int
    error: str | None = None


# --- User-driven SAM2 segmentation ---------------------------------------
# The operator clicks somewhere on the snapshot; SAM2 returns a polygon
# of whatever's under that click. Optional negative clicks let the user
# carve out parts they didn't want. `prev_polygon` enables iterative
# refinement — pass back the polygon you got from the last call so SAM2
# can adjust it instead of starting from scratch.


class SegmentAtPointIn(BaseModel):
    positive: list[list[float]] = Field(..., min_length=1, max_length=64)  # normalized [0,1]
    negative: list[list[float]] = Field(default_factory=list, max_length=64)
    prev_polygon: list[list[float]] | None = Field(None, max_length=2048)

    @field_validator("positive", "negative", "prev_polygon")
    @classmethod
    def _points(cls, v: list[list[float]] | None) -> list[list[float]] | None:
        return _validate_points(v) if v else v


class SegmentAtPointOut(BaseModel):
    polygon: list[list[float]]
    latency_ms: int
    error: str | None = None


# Optional Claude-driven naming of a polygon the user has drawn or
# segmented. Cheap (one VLM call with a small image crop) and lets us
# move the "what is this?" guesswork off the user.


class ClassifyPolygonIn(BaseModel):
    polygon: list[list[float]] = Field(..., min_length=3, max_length=2048)  # normalized [0,1]
    locale: str = "hr"
    provider: str | None = None

    @field_validator("polygon")
    @classmethod
    def _points(cls, v: list[list[float]]) -> list[list[float]]:
        return _validate_points(v)


class ClassifyPolygonOut(BaseModel):
    name: str
    kind: ZoneKind
    rationale: str = ""
    model: str
    latency_ms: int
    error: str | None = None


# --- scene-state regions (gate open/closed, door up/down, …) ----------------
# A scene region is a fixed polygon whose *persistent state* the state-evaluator
# classifies via DINOv2 few-shot prototypes. Distinct from zones (which test
# transient tracks) — see db/migrations/042_scene_states.sql.


class SceneRegionIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    # The physical thing observed, when more than one camera can see it. Two
    # regions sharing a place are views of one spot.
    place: str | None = Field(None, max_length=64)
    polygon: list[list[float]] = Field(..., min_length=3, max_length=64)
    # Ordered state labels, e.g. ["open", "closed"]. Evaluation needs >= 2
    # labelled states with prototypes before it classifies; an empty list is
    # allowed at create time so the operator can add states in the editor.
    states: list[str] = Field(default_factory=list, max_length=16)
    sample_interval_s: int = Field(8, ge=1, le=3600)
    hysteresis_n: int = Field(3, ge=1, le=20)
    unknown_margin: float = Field(0.40, ge=0.0, le=2.0)
    color: str = Field("#22d3ee", pattern=_COLOR_PATTERN)
    enabled: bool = True


class SceneRegionPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=64)
    place: str | None = Field(None, max_length=64)
    polygon: list[list[float]] | None = Field(None, min_length=3, max_length=64)
    states: list[str] | None = Field(None, max_length=16)
    sample_interval_s: int | None = Field(None, ge=1, le=3600)
    hysteresis_n: int | None = Field(None, ge=1, le=20)
    unknown_margin: float | None = Field(None, ge=0.0, le=2.0)
    color: str | None = Field(None, pattern=_COLOR_PATTERN)
    enabled: bool | None = None


class ScenePrototype(BaseModel):
    id: UUID
    state_label: str
    crop_path: str | None
    captured_at: datetime


class SceneRegionStatus(BaseModel):
    current_state: str | None = None
    current_state_since: datetime | None = None
    last_eval_at: datetime | None = None
    last_label_raw: str | None = None
    last_distance: float | None = None


class SceneRegion(BaseModel):
    id: UUID
    camera_id: UUID
    name: str
    place: str | None = None
    polygon: list[list[float]]
    states: list[str]
    sample_interval_s: int
    hysteresis_n: int
    unknown_margin: float
    color: str
    enabled: bool
    created_at: datetime
    updated_at: datetime
    status: SceneRegionStatus | None = None
    prototypes: list[ScenePrototype] = Field(default_factory=list)


class SceneCaptureIn(BaseModel):
    state_label: str = Field(..., min_length=1, max_length=32)
    # Take the reference from the recording covering this moment instead of
    # from the live ring. The state worth teaching is rarely the one on screen
    # right now (a gate you want labelled "open" is closed today; the car whose
    # spot you are enrolling left an hour ago). None = live.
    at: datetime | None = None


class SceneCaptureOut(BaseModel):
    prototype_id: UUID | None = None
    crop_path: str | None = None
    error: str | None = None


class SceneEvalOut(BaseModel):
    state: str | None = None
    raw_label: str | None = None
    distance: float | None = None
    per_state: dict[str, float] = Field(default_factory=dict)
    error: str | None = None


# --- Telemetry incidents (pipeline flight recorder + AI verdicts) ----------
# Watchers in the event-manager open incidents over camera_telemetry
# buckets; the operator asks the AI for a verdict (analyze) and either
# applies its constrained suggestion set or dismisses. `IncidentChange`
# is the ONLY shape apply accepts — free-form LLM output never touches
# the database directly.


class IncidentChange(BaseModel):
    target: str = Field(..., pattern=r"^(camera_rule|camera)$")
    # camera_rule → which class the threshold applies to.
    class_name: str | None = Field(None, min_length=1, max_length=64)
    field: str = Field(..., min_length=1, max_length=32)
    value: float
    reason: str = Field("", max_length=500)


class IncidentOut(BaseModel):
    id: UUID
    camera_slug: str
    kind: str
    opened_at: datetime
    closed_at: datetime | None = None
    status: str
    details: dict[str, Any] = Field(default_factory=dict)
    verdict: str | None = None
    suggestion: dict[str, Any] | None = None


class IncidentAnalyzeOut(BaseModel):
    model: str
    latency_ms: int
    error: str | None = None
    incident: IncidentOut | None = None


class TelemetrySettings(BaseModel):
    # When on, the api's auto-analyzer runs the AI verdict on every incident
    # the watchers open — each analysis is one paid vision call, so this is
    # an explicit operator opt-in, not a default.
    auto_analyze: bool = False


class TelemetryUsage(BaseModel):
    """Cumulative token spend of incident analyses (manual + auto)."""

    analyses: int
    input_tokens: int
    output_tokens: int
