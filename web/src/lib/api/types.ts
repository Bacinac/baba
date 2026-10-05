// API DTOs and shared types. Split out of the old monolithic api.ts.

import type { AnalysisStream, StreamPreset } from "$lib/streamPresets";

export type { AnalysisStream, StreamPreset };

export interface Camera {
  id: string;
  slug: string;
  name: string;
  /** What the camera records. */
  stream_url: string;
  /** The low-resolution substream, when the camera exposes both. */
  substream_url: string | null;
  /** The stream detection, tracking and embedding read. */
  analysis_stream: AnalysisStream;
  enabled: boolean;
  target_fps: number;
  /** Adaptive detection rate: fps while the scene is quiet (tracker reports
   *  only parked/no objects); ramps back to `target_fps` on the first fresh
   *  detection. Null → adaptive off, constant `target_fps`. */
  idle_fps: number | null;
  /** Tracker stillness: object-relative radius (fraction of bbox diagonal)
   *  the centre may wander while still counting as motionless. Wider for
   *  occlusion-split scenes (pillar cutting a parked car in two boxes).
   *  Null = inherits the global tracking defaults (Settings → Detection). */
  stillness_ratio: number | null;
  /** Stillness seconds before the stationary→parked promotion. Null = global. */
  park_seconds: number | null;
  /** Seconds a track coasts without a matching detection before it dies.
   *  Null = global. */
  lost_seconds: number | null;
  /** Seconds a dead track stays appearance-resurrectable with the SAME id
   *  (OSNet re-attach). Null = global. */
  reid_lost_seconds: number | null;
  /** Two-threshold MAINTAIN floor — per-camera (NOT NULL, no global fallback),
   *  light-profiled so night IR can lower it without touching birth. */
  maintain_conf: number;
  /** Measured illumination band (frame-measured luma/IR, written by the
   *  event-manager): ir | dark | dim | normal | bright. Profiles of
   *  lighting-sensitive settings are keyed by this. */
  light_condition: string;
  downscale_max_edge: number;
  recording_enabled: boolean;
  /** Operator-pickable identity color for this camera, used to tint
   *  the storage breakdown bar and (in time) the live mosaic border,
   *  event chips, and analytics legends. `#RRGGBB`. */
  color: string;
  created_at: string;
  updated_at: string;
}

export interface CameraIn {
  slug: string;
  name: string;
  stream_url: string;
  substream_url?: string | null;
  analysis_stream?: AnalysisStream;
  enabled?: boolean;
  target_fps?: number;
  idle_fps?: number | null;
  stillness_ratio?: number | null;
  park_seconds?: number | null;
  lost_seconds?: number | null;
  reid_lost_seconds?: number | null;
  maintain_conf?: number | null;
  downscale_max_edge?: number;
  recording_enabled?: boolean;
  color?: string;
}

export interface CameraPatch {
  name?: string;
  stream_url?: string;
  substream_url?: string | null;
  analysis_stream?: AnalysisStream;
  enabled?: boolean;
  target_fps?: number;
  idle_fps?: number | null;
  /** Explicit null clears the per-camera override → inherit global. */
  stillness_ratio?: number | null;
  park_seconds?: number | null;
  lost_seconds?: number | null;
  reid_lost_seconds?: number | null;
  maintain_conf?: number | null;
  downscale_max_edge?: number;
  recording_enabled?: boolean;
  color?: string;
}

/** Global live-view overlay toggles. `boxes` draws detection rectangles on
 *  the grid stills + detail-view canvas; `badges` shows per-tile detection
 *  chips on the grid. */
export interface LiveOverlay {
  boxes: boolean;
  badges: boolean;
}

/** One class's current detection on a camera (live-grid badge). */
export interface LiveDetection {
  class_name: string;
  class_id: number;
  count: number;
  moving: boolean;
}

/** Per-camera live status for the grid: measured effective fps + current
 *  detections. `fps` is the real message-arrival rate (adaptive), 0 when the
 *  camera is quiet. */
export interface LiveStatus {
  fps: number;
  detections: LiveDetection[];
}

/** Global tracking defaults — the base layer every camera inherits unless
 *  it carries a per-camera override (Settings → Detection → Praćenje). */
export interface TrackingDefaults {
  stillness_ratio: number;
  park_seconds: number;
  lost_seconds: number;
  reid_lost_seconds: number;
}

export interface TestConnectionResult {
  ok: boolean;
  codec: string | null;
  width: number | null;
  height: number | null;
  fps: number | null;
  duration_ms: number;
  error: string | null;
}

export interface DiscoveredCamera {
  ip: string;
  open_ports: number[];
  signals: string[];   // e.g. ["rtsp:554", "onvif:80"]
}

export interface DiscoverResult {
  duration_ms: number;
  scanned: number;
  found: DiscoveredCamera[];
}

export interface BabaUser {
  id: string;
  username: string;
  role: "admin" | "operator" | "viewer";
  created_at: string;
  updated_at: string;
  last_login_at: string | null;
  totp_enabled: boolean;
}

// Backend whitelist: only these keys round-trip through GET/PATCH /auth/preferences.
// `null` on PATCH explicitly clears the key; missing keys are left alone.

export interface UserPreferences {
  default_landing?: string | null;
  time_format_24h?: boolean | null;
  timezone?: string | null;
  theme?: "light" | "dark" | "system" | null;
  locale?: "hr" | "en" | null;
  /** Seconds of lead-in before an event when playing its clip. */
  clip_preroll_s?: number | null;
  /** Seconds of tail after an event when playing its clip. */
  clip_postroll_s?: number | null;
  /** Autoplay the clip when an event is opened. */
  clip_autoplay?: boolean | null;
  /** Default time range when opening the Activity page. */
  activity_default_range?: "hour" | "today" | "yesterday" | "7d" | "all" | null;
}

export interface StreamCandidate {
  template_id: string;
  label: string;
  stream_url: string;
  codec: string | null;
  width: number | null;
  height: number | null;
  fps: number | null;
  snapshot_b64: string | null;
}

export interface ProbeResult {
  ip: string;
  vendor: string | null;
  model: string | null;
  duration_ms: number;
  candidates: StreamCandidate[];
}

export interface BabaEvent {
  id: string;
  kind: string;
  at: string;  // ISO timestamp
  payload: Record<string, unknown>;
  camera: { id: string; slug: string; name: string };
  track: {
    id: string;
    class_id: number;
    class_name: string;
    duration_s: number;
    /** Relative path like 'thumbnails/<uuid>.jpg' served by /api/<path>. Null if capture failed. */
    thumbnail_path: string | null;
  } | null;
  /** Playback info for this event, if a recording covers it. */
  recording: RecordingRef | null;
}

/** How the UI plays back a visit. `start_at`/`end_at` is the absolute window
 * the clip player cuts from (spanning whatever segments it covers); `id` +
 * `seek_seconds` locate the moment inside that window. */

export interface RecordingRef {
  id: string;
  seek_seconds: number;
  start_at: string;  // ISO — visit start (absolute)
  end_at: string;    // ISO — visit end (absolute)
}

/** A track the pipeline followed and a filter kept out of the feed. Carries no
 *  thumbnail and no identity — skipping that work is what the filter is for —
 *  so it answers one question: the camera saw something here, and this is what
 *  hid it. */
export interface SuppressedTrack {
  id: string;
  camera: { id: string; slug: string; name: string };
  class_name: string;
  started_at: string;
  ended_at: string;
  observations: number;
  suppressed_reason: "off-zone" | "sub-dwell" | "static";
}

export interface Sighting {
  id: string;
  camera: { id: string; slug: string; name: string };
  global_id: string;
  identity: { name: string; kind: string | null } | null;
  class_name: string;
  started_at: string;
  ended_at: string;
  duration_s: number;
  track_count: number;
  observations: number;
  face_verified: boolean;
  plate_text?: string | null;
  /** kind === "left_with_vehicle" only: the linked car's display name. */
  vehicle_name?: string | null;
  /** On a vehicle's own departure row: the person who drove off in it. The
   *  walk-out and the drive-away are one departure, so it rides the row that
   *  has the picture instead of adding a blank one beside it. */
  left_with?: string | null;
  /** Scene thumbnail with the detection box already baked in (event-manager
   *  SnapshotBaker draws it on the detection's own SHM frame). */
  thumbnail_path: string | null;
  crop_path: string | null;
  track_ids: string[];
  /** A parked vehicle splits into two feed rows at their own moments: `arrival`
   *  (drive-in + settle) and `departure` (wake + drive-off). Everything else is
   *  one `visit` row. started_at/ended_at IS the clip window for that row. */
  kind: "visit" | "arrival" | "departure" | "left_with_vehicle";
  /** The full visit length, for context on a split row ("parked 4h20m"). */
  visit_duration_s: number;
  zones: {
    zone_id: string;
    zone_name: string | null;
    zone_kind: string | null;
    first_enter: string | null;
    last_exit: string | null;
    /** True time inside = sum of paired enter→exit intervals. NOT last_exit −
     *  first_enter, which overcounts when a zone is re-touched much later. */
    dwell_s: number;
  }[];
  recording: RecordingRef | null;
}

export interface SightingsFilter {
  camera_id?: string;
  since?: string;
  until?: string;
  limit?: number;
  class_name?: string;
  identity?: string;
}

/** Build a fully-qualified URL for a thumbnail relative path returned by the API. */


/** One recording coverage run as returned by GET /recordings/coverage. Server
 * orders by started_at DESC. `ended_at` and `size_bytes` are null while the
 * segment is still being written by ffmpeg. */

export interface Recording {
  id: string;
  camera: { id: string; slug: string; name: string };
  started_at: string;     // ISO
  ended_at: string | null;
  duration_s: number | null;
  size_bytes: number | null;
}

export interface RecordingsFilter {
  camera_id?: string;
  since?: string;
  until?: string;
  limit?: number;
}

/** Global recording policy (Settings → Recording). */

export interface RecordingSettings {
  mode: "continuous" | "activity";
  retention_days: number;
  disk_high_water_pct: number;
  disk_low_water_pct: number;
  activity_buffer_minutes: number;
  updated_at: string;
  disk: DiskUsage | null;
  /** Second storage tier: the small-file dirs (crops, clip cache, thumbnails,
   *  identity/scene photos) when they sit on a different volume than the bulk
   *  segments. null when both live on one device — the UI then shows one bar. */
  disk_fast: DiskUsage | null;
}

export interface DiskUsage {
  total_bytes: number;
  used_bytes: number;
  free_bytes: number;
  used_pct: number;
}

export type RecordingSettingsIn = Omit<RecordingSettings, "updated_at" | "disk" | "disk_fast">;

export type PurgeScope = "recordings" | "record";

export interface PurgeRecordingsResult {
  scheduled: boolean;
  scope: PurgeScope;
  pending_rows: number;
  pending_bytes: number;
  pending_events: number;
}

// --- Identities (re-ID clusters) -----------------------------------------

/** Operator-assigned metadata attached to an identity. Null when the
 *  identity has not been named yet — the UI then falls back to a short
 *  uuid for display. The full detail-page version of this includes more
 *  fields (notes, reference_count, audit cols). */

export interface IdentityLabelSummary {
  name: string;
  kind: string | null;
  tags: string[];
  /** Vocabulary-bound, NOT a tag: automations key on this, and free-text tags
   *  had already drifted (one identity carried both `obitelj` and `family`,
   *  another only `obitelj`, so filtering "family" silently missed it). */
  affiliation:
    | "family" | "friend" | "neighbour" | "guest"
    | "delivery" | "service" | "official" | "unknown" | string;
  /** Orthogonal to affiliation, not a value of it: Nika is family but no
   *  longer lives here; a lodger is resident without being family. */
  resident: boolean;
  /** For pets: what the animal IS. The detector flips cat<->dog on the same
   *  animal (why PET_GROUP pools them for re-ID), so the identity owns this. */
  species: string | null;
  plate: string | null;
  /** vehicles only: owning person identity (global_id) — departures close the
   *  owner's presence; never used to name anyone. */
  linked_person?: string | null;
  /** The OPUS · Library person this identity stands for; null when not linked. */
  opus_person_id?: number | null;
  /** 'manual' = operator-typed, 'ai' = AI draft not yet reviewed,
   *  'ai_reviewed' = AI draft the operator has since edited.
   *  The gallery shows a "draft" affordance for 'ai' only. */
  source: "manual" | "ai" | "ai_reviewed" | string;
  has_reference_embedding: boolean;
}

export interface IdentityLabel extends IdentityLabelSummary {
  notes: string | null;
  reference_count: number;
  ai_described_at: string | null;
  /** Operator-pinned cover image (media-relative path). When set, the
   *  detail/list endpoints use this in place of the auto-picked
   *  latest_crop. Null = fall back to default heuristic. */
  cover_photo_path: string | null;
}

/** Multi-modal re-ID match modality. body = DINOv2 whole-body,
 *  face = AuraFace facial features (outfit-invariant). */

export type IdentityMatchModality = "body" | "face";

/** One row in identity_reference_photos. `photo_path` is null for
 *  back-compat rows synthesised from pre-migration aggregated
 *  embeddings — those have no original to display. */

export interface IdentityReferencePhoto {
  id: string;
  photo_path: string | null;
  has_face: boolean;
  /** Contributes a body (OSNet) vector. For a person this is only ever true
   *  when `has_face` is too — a torso with no readable face is nobody in
   *  particular, and enrolling it widens the identity toward whoever it
   *  actually was. Pets and vehicles are body-only by nature. */
  has_body: boolean;
  /** Where the photo came from: upload | from-tracks | auto-select |
   *  from-face-samples | immich | opus. */
  source: string;
  /** Shorter side of the detected face box, in pixels of the stored photo.
   *  null for references enrolled before this was recorded. */
  face_px: number | null;
  face_bbox: number[] | null;
  uploaded_at: string;
  uploaded_by: string | null;
}

/** Aggregate view of one re-ID identity. One row per `global_id`. */

export interface IdentitySummary {
  global_id: string;
  class_id: number;
  class_name: string;
  n_tracks: number;
  first_seen: string;
  last_seen: string;
  cameras: string[];
  thumbnail_path: string | null;
  /** Focused 224x224 JPEG of the subject — the same pixels DINOv2 saw
   *  when it computed the canonical embedding. Prefer this over
   *  thumbnail_path for display; thumbnail_path is the wider scene used
   *  only as context when needed. */
  crop_path: string | null;
  /** Cosine distance to the `near` query identity's canonical embedding.
   *  Only populated when the request set `near=<gid>`; smaller = more similar. */
  nearest_dist: number | null;
  /** True when this identity has face evidence -- a captured face on any of
   *  its sightings, or an enrolled face reference. The "face-confirmed vs
   *  appearance-only" trust signal: a face-confirmed identity is valid across
   *  sessions; an unconfirmed one was grouped by body appearance only. */
  face_confirmed: boolean;
  label: IdentityLabelSummary | null;
}

/** Per-track row inside an identity detail. */

export interface IdentityTrack {
  id: string;
  camera: { id: string; slug: string; name: string };
  class_id: number;
  class_name: string;
  started_at: string;
  ended_at: string;
  duration_s: number | null;
  n_observations: number;
  thumbnail_path: string | null;
  crop_path: string | null;
  /** A face was captured + embedded for this specific sighting, so it can be
   *  face-verified. Drives the per-sighting face badge in the timeline. */
  has_face: boolean;
}

// One stay of a named person on one camera — the presence registry's record,
// not a track. `departed_at` null = still there; `closed_by` says why it ended
// ('elsewhere' | 'absence' | 'stale' | 'vehicle').
export interface PresenceEpisode {
  camera_id: string;
  camera_name: string;
  evidence: "face" | "body";
  present_since: string;
  departed_at: string | null;
  closed_by: string | null;
  duration_s: number;
}

export interface IdentityDetail extends Omit<IdentitySummary, "label"> {
  tracks: IdentityTrack[];
  /** Detail label includes the larger free-form fields (notes,
   *  reference_count). The list endpoint trims those for payload size. */
  label: IdentityLabel | null;
}

export interface IdentityLabelIn {
  name?: string;
  kind?: string | null;
  tags?: string[];
  affiliation?: string | null;
  resident?: boolean | null;
  species?: string | null;
  notes?: string | null;
  plate?: string | null;
  /** vehicles only: the owning person identity (global_id); departures close
   *  the owner's presence. Never used to name anyone. */
  linked_person?: string | null;
  /** The OPUS · Library person this identity stands for. Null unlinks. */
  opus_person_id?: number | null;
}

export interface ReferencePhotoUploadResult {
  label: IdentityLabel & {
    global_id: string;
    created_by: string | null;
    created_at: string;
    updated_at: string;
  };
  used: number;
  /** How many of the kept photos yielded a face embedding. Only the face
   *  vector is a cross-session identity signal, so this — not `used` — is
   *  what says whether an enrollment actually bought recognition power. */
  faces_detected: number;
  skipped: string[];
}

/** Which external face-reference libraries this installation has set up. */
export interface ReferenceSources {
  opus: boolean;
  immich: boolean;
}

/** Connection state for the Immich photo library we import face references
 *  from. The API key is never echoed back — `configured` is the only thing
 *  that says one is stored. `reachable` is a live ping, so a false here with
 *  `configured: true` means Immich is down or the key was revoked. */
export interface ImmichStatus {
  configured: boolean;
  reachable: boolean;
  base_url: string | null;
  version: string | null;
  /** Present only when configured but unreachable. */
  error?: string;
}

export interface ImmichSettingsIn {
  base_url: string;
  /** Null keeps the stored key. It is never sent back to the browser, so a
   *  form that demanded it forced the operator to re-fetch it from Immich
   *  just to fix a typo in the address. */
  api_key: string | null;
}

/** A NAMED person in Immich. Unnamed face clusters are filtered out server
 *  side — they carry no information we could enroll. */
export interface ImmichPerson {
  id: string;
  name: string;
}

export interface ImmichPeopleResult {
  people: ImmichPerson[];
  total: number;
}

export interface ImmichImportResult extends ReferencePhotoUploadResult {
  immich_person: string;
}

/** Connection state for OPUS · Library, the household photo catalogue we
 *  import face references from. The token is never echoed back —
 *  `configured` is the only thing that says one is stored. `reachable` is a
 *  live ping, so a false here with `configured: true` means the library is
 *  down or the token was rotated. */
export interface OpusStatus {
  configured: boolean;
  reachable: boolean;
  base_url: string | null;
  /** Named people in the library; null when unreachable. */
  people: number | null;
  /** Present only when configured but unreachable. */
  error?: string;
}

export interface OpusSettingsIn {
  base_url: string;
  /** Null keeps the stored token. */
  token: string | null;
}

/** A named person in the library. */
export interface OpusPerson {
  id: number;
  name: string;
  faces: number;
  /** First and last year photographed; nulls when undated. */
  years: [number | null, number | null];
  /** The library's face id for the person's best portrait, or null. */
  cover: number | null;
  /** The identity here that already stands for this person, if any. */
  identity: string | null;
  /** An unlinked person identity of the same name (full or given): the
   *  picker links and enrols it rather than creating a twin. */
  same_name: string | null;
}

export interface OpusPeopleResult {
  people: OpusPerson[];
  total: number;
}

export interface OpusImportResult extends ReferencePhotoUploadResult {
  opus_person: string;
}

export interface OpusNewIdentityResult extends OpusImportResult {
  global_id: string;
}

/** Structured suggestion from the configured AI provider's vision API.
 *  Fields mirror what the label editor accepts. `confidence` is a
 *  free-form hint the model decided ("low"|"medium"|"high"); the UI
 *  uses it only to colour the suggestion preview. */

export interface IdentityAiSuggestion {
  name?: string;
  kind?: string;
  /** Pets only. The model is asked for this SEPARATELY from tags because the
   *  detector flips cat<->dog on the same animal, so the identity must own it.
   *  It is never asked for affiliation/residency — it cannot know that. */
  species?: string | null;
  tags?: string[];
  description?: string;
  plate?: string | null;
  confidence?: "low" | "medium" | "high" | string;
}

export type IdentityAiDescribeResult =
  | {
      ok: true;
      suggestion: IdentityAiSuggestion;
      model: string;
      latency_ms: number;
      /** Relative path of the image actually sent to the VLM — usually
       *  the focused crop_path, falls back to thumbnail_path. */
      image_path: string;
    }
  | {
      ok: false;
      raw: string | null;
      model: string;
      latency_ms: number;
      error: string;
    };

/** One occupancy episode of a parking place — who stood where and how long.
 *  `name` null = occupied by an unidentified vehicle (an honest record). */
export interface ParkingEpisode {
  place: string;
  name: string | null;
  evidence: "plate" | "body" | "unknown";
  occupied_since: string;
  released_at: string | null;
  duration_s: number;
}

export interface IdentitiesFilter {
  class_id?: number;
  since?: string;
  limit?: number;
  /** When set, server sorts candidates by cosine distance to this identity's
   *  canonical embedding and excludes it from the result. */
  near?: string;
  /** true → only labeled, false → only anonymous, undefined → both. */
  labeled?: boolean;
  /** Case-insensitive substring match against identity_labels.name. */
  q?: string;
  /** true (default in UI) → hide identities with no thumbnail / crop. */
  has_image?: boolean;
}


/** Frontend mirror of `BABA_EVENT_REID_COSINE_THRESHOLD`. Anything
 *  below this distance was already auto-merged at finalize, so a
 *  candidate scoring under it in the merge picker is a "we *would*
 *  have linked this if we'd seen it together" near-miss — worth
 *  flagging visually. Keep in sync with event-manager's default. */

export interface AiSettings {
  provider: string;
  model: string;
  api_key_masked: string;
  enabled: boolean;
  last_used_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface AiSettingsIn {
  provider: string;
  model: string;
  /** Omit (or null) on update to keep the existing key. */
  api_key?: string | null;
  enabled?: boolean;
}

export interface AiTestResult {
  ok: boolean;
  model: string | null;
  latency_ms: number;
  error: string | null;
}

export interface AiDefaults {
  providers: string[];
  anthropic_default_model: string;
  anthropic_models: string[];
  openai_default_model: string;
  openai_models: string[];
}

// --- Face recognition models / settings ---

export interface FaceModel {
  key: string;
  name: string;
  description: string;
  license: string;
  tier: "default" | "byom" | "commercial";
  bundled_with_baba: boolean;
  file_present: boolean;
  model_filename: string;
  input_size: number;
  embedding_dim: number;
}

export interface FaceDetector {
  key: string;
  name: string;
  description: string;
  license: string;
  tier: "default" | "byom";
  bundled_with_baba: boolean;
  file_present: boolean;
  model_filename: string;
}

export interface FaceRecognitionSettings {
  model_key: string;
  detector_key: string;
  match_threshold: number;
  updated_at: string;
  updated_by_user: string | null;
  references_in_active_model: number;
  references_total: number;
  /** Photos with no face at all — pets, vehicles, or shots where none was
   *  found. Matched by body embedding; recompute cannot give them a face. */
  references_no_face: number;
  /** Photos that DO carry a face embedding, but from another model. These are
   *  the only ones Force recompute actually fixes. */
  references_stale: number;
}

export interface FaceRecognitionSettingsIn {
  model_key: string;
  detector_key: string;
  match_threshold: number;
}

export interface FaceRecomputeStatus {
  job_id: string;
  model_key: string;
  status: "running" | "done" | "failed" | "cancelled";
  total: number;
  processed: number;
  succeeded: number;
  no_face: number;
  missing_file: number;
  started_at: string;
  finished_at: string | null;
  error_message: string | null;
}

export interface ZoneSuggestion {
  name: string;
  kind: string;
  polygon: [number, number][];
  color: string;
  rationale: string;
}

export interface SuggestZonesResult {
  model: string;
  suggestions: ZoneSuggestion[];
  latency_ms: number;
  error: string | null;
}

// --- AI auto-tune zone rules --------------------------------------------

export interface ZoneClassStatsPayload {
  count: number;
  conf_p25: number;
  conf_p50: number;
  conf_p75: number;
  conf_max: number;
  area_p50: number;
}

export interface ZoneObservationPayload {
  zone_id: string;
  zone_name: string;
  zone_kind: string;
  current_rules: unknown | null;
  class_stats: Record<string, ZoneClassStatsPayload>;
}

export interface TuneZoneRulesRequest {
  duration_s: number;
  frame_width: number;
  frame_height: number;
  zones: ZoneObservationPayload[];
}

export interface ProposedClassRule {
  min_confidence: number | null;
  min_area_pct: number | null;
  min_dwell_ms: number | null;
  /** Backend gate: "moving_only" fires only for a track whose motion state
   *  is `active`. Person is exempt. Carried so the zone editor's preview
   *  can answer the same question the event-manager does. */
  motion_gate?: "moving_only" | null;
  cooldown_s: number | null;
}

export interface ProposedZoneRules {
  zone_id: string;
  zone_name: string;
  enabled_classes: Record<string, ProposedClassRule>;
  rationale: string;
}

export interface TuneZoneRulesResult {
  model: string;
  proposals: ProposedZoneRules[];
  latency_ms: number;
  error: string | null;
}

export interface SegmentAtPointRequest {
  positive: [number, number][];
  negative?: [number, number][];
  prev_polygon?: [number, number][] | null;
}

export interface SegmentAtPointResult {
  polygon: [number, number][];
  latency_ms: number;
  error: string | null;
}

export interface ClassifyPolygonResult {
  name: string;
  kind: string;
  rationale: string;
  model: string;
  latency_ms: number;
  error: string | null;
}

// --- Zones ----------------------------------------------------------------

/** `ignore` is the only kind the pipeline acts on structurally: the tracker
 *  refuses to emit any track whose foot position falls inside one (see
 *  services/tracker zone_masks.py). The rest are labels the event-manager
 *  reads for motion gating and display. */
export type ZoneKind =
  | "entry"
  | "exit"
  | "restricted"
  | "parking"
  | "no_go"
  | "interest"
  | "generic"
  | "ignore";

/** Per-class refinement inside a zone — matches db/migrations/034
 *  zones.rules.enabled_classes.<class_name>.  All four fields optional;
 *  unset values inherit camera/global defaults. */

export interface ZoneClassRule {
  min_confidence?: number | null;
  min_area_pct?: number | null;
  min_dwell_ms?: number | null;
  motion_gate?: "moving_only" | null;
  cooldown_s?: number | null;
}

export interface ZoneRules {
  /** Operator's per-zone allowlist.  Empty/absent ⇒ no restriction
   *  beyond what camera + global already enforce. */
  enabled_classes: Record<string, ZoneClassRule>;
}

export interface Zone {
  id: string;
  camera_id: string;
  name: string;
  kind: ZoneKind | string;
  polygon: [number, number][];
  color: string;
  enabled: boolean;
  rules: ZoneRules;
  created_at: string;
  updated_at: string;
}

export interface ZoneIn {
  name: string;
  kind?: string;
  polygon: [number, number][];
  color?: string;
  enabled?: boolean;
  rules?: ZoneRules;
}

export interface ZonePatch {
  name?: string;
  kind?: string;
  polygon?: [number, number][];
  color?: string;
  enabled?: boolean;
  rules?: ZoneRules;
}

// --- Detection rules (layers 1 + 2) ---------------------------------------

export interface DetectionClass {
  class_id: number;
  name: string;
  group: "person" | "vehicle" | "animal" | "other" | string;
}

export interface GlobalDetectionRule {
  class_name: string;
  min_confidence: number;
  enabled: boolean;
  /** Minimum bbox size as % of the frame (max of w/frame_w, h/frame_h).
   *  0 = no size floor. */
  min_box_pct: number;
}

/** Resolved detection rule for one camera + class, computed server-side by
 *  `baba_core.rule_resolve` — the same function the detector uses. `source`
 *  distinguishes an inherited global from a per-camera override and, above
 *  all, from `not-allowed`: a class with no global row is dropped outright
 *  and a per-camera override CANNOT rescue it. */
export interface EffectiveDetectionRule {
  class_name: string;
  enabled: boolean;
  min_confidence: number;
  min_box_pct: number;
  source: "unconfigured" | "global" | "override" | "not-allowed";
}

/** One operator-tunable pipeline threshold. The form is GENERATED from these
 *  — range, label key and all — so adding a knob in `baba_core.tunables`
 *  surfaces it here with no UI change. `is_set` false means the deployment
 *  default is in force, which is not the same as "happens to equal it". */
export interface Tunable {
  name: string;
  group: string;
  value: number;
  default: number;
  lo: number;
  hi: number;
  help_key: string;
  is_set: boolean;
}

export interface CameraDetectionRule {
  camera_id: string;
  class_name: string;
  /** null means "inherit global". */
  min_confidence: number | null;
  /** null means "inherit global". */
  enabled: boolean | null;
  /** null means "inherit global". */
  min_box_pct: number | null;
}

export interface SearchHit {
  track_id: string;
  global_id: string | null;
  camera_id: string;
  camera_name: string;
  class_id: number;
  class_name: string | null;
  distance: number;
  thumbnail_path: string | null;
  crop_path: string | null;
  label_name: string | null;
  started_at: string;
  ended_at: string | null;
}

export interface SearchResult {
  hits: SearchHit[];
  total_considered: number;
}

// --- audit log types ---

export interface AuditEntry {
  id: string;
  at: string;
  user_id: string | null;
  username: string | null;
  resource_type: string;
  resource_id: string | null;
  op: string;
  payload: Record<string, unknown>;
}

/** What the log holds; the words for each come from baba_api.audit's vocabulary. */
export interface AuditFacets {
  resource_types: string[];
  ops: string[];
}

export interface AuditPurgeResult {
  deleted: number;
  older_than: string;
}

// --- notification channels types ---

export type NotificationKind = "webhook" | "slack" | "telegram" | "smtp";

export interface NotificationChannel {
  id: string;
  name: string;
  kind: NotificationKind;
  enabled: boolean;
  target_preview: string | null;
  last_used_at: string | null;
  last_error: string | null;
  // 0 = unlimited. Otherwise the dispatcher caps fanout to this many
  // deliveries per minute per channel; excess is recorded with
  // ok=false, error="rate_limited" in notification_deliveries.
  rate_limit_per_min: number;
  created_at: string;
  updated_at: string;
}

export interface NotificationChannelIn {
  name: string;
  kind: NotificationKind;
  enabled?: boolean;
  // Per-kind config. The backend validates shape against the kind:
  //   webhook:  { url: string; method?: string; headers?: Record<string,string> }
  //   slack:    { url: string }   (hooks.slack.com/...)
  //   telegram: { bot_token: string; chat_id: string }
  //   smtp:     { host: string; port?: number; username?: string;
  //               password?: string; from: string; to: string; use_tls?: boolean }
  config: Record<string, unknown>;
  target_preview?: string | null;
  rate_limit_per_min?: number;
}

export interface NotificationTestResult {
  ok: boolean;
  error: string | null;
}

// --- notification rules types ---

export interface NotificationRuleFilter {
  camera_ids?: string[];
  class_ids?: number[];
}

export interface NotificationRule {
  id: string;
  name: string;
  enabled: boolean;
  event_kind: string | null;
  filter: NotificationRuleFilter;
  channel_ids: string[];
  last_fired_at: string | null;
  last_error: string | null;
  created_at: string;
  updated_at: string;
}

export interface NotificationRuleIn {
  name: string;
  enabled?: boolean;
  event_kind?: string | null;
  filter?: NotificationRuleFilter;
  channel_ids?: string[];
}

// --- delivery audit + analytics ---

export interface DeliveryRow {
  id: string;
  at: string;
  rule_id: string | null;
  rule_name: string | null;
  channel_id: string | null;
  channel_name: string | null;
  event_id: string | null;
  event_kind: string;
  channel_kind: string;
  ok: boolean;
  error: string | null;
  duration_ms: number | null;
}

export interface DeliveriesFilter {
  rule_id?: string;
  channel_id?: string;
  ok?: boolean;
  since?: string;
  until?: string;
  limit?: number;
}

export interface HourBucket {
  bucket: string;
  count: number;
}

export interface EventsByHourOut {
  since: string;
  until: string;
  buckets: HourBucket[];
}

export interface CameraCount {
  camera_id: string;
  slug: string;
  name: string;
  count: number;
}

export interface EventsByCameraOut {
  since: string;
  until: string;
  rows: CameraCount[];
}

export interface KindCount {
  kind: string;
  count: number;
}

export interface EventsByKindOut {
  since: string;
  until: string;
  rows: KindCount[];
}

export type HeatmapClassGroup = "all" | "person" | "vehicle" | "animal" | "other";

export interface HeatmapOut {
  camera_id: string;
  days: number;
  class_group: HeatmapClassGroup;
  grid_w: number;
  grid_h: number;
  cells: number[];
  max_count: number;
}

export interface AuditFilter {
  resource_type?: string;
  resource_id?: string;
  user_id?: string;
  op?: string;
  since?: string;
  until?: string;
  limit?: number;
}

// --- stats / system / storage types ---

export interface Stats {
  cameras_total: number;
  cameras_enabled: number;
  events_total: number;
  events_24h: number;
  identities_total: number;
  identities_labeled: number;
  tracks_total: number;
  recordings_total: number;
  recordings_bytes: number;
  cameras_active_24h: number;
  active_camera_ids_24h: string[];
}

export interface CameraStorage {
  camera_id: string;
  slug: string;
  name: string;
  color: string;
  segments: number;
  bytes: number;
  oldest_at: string | null;
  newest_at: string | null;
}

export interface StorageUsage {
  media_path: string;
  media_total_bytes: number;
  media_free_bytes: number;
  cameras: CameraStorage[];
}

// App revision (GET /version). Git-derived: base MAJOR.MINOR from the VERSION
// file, patch = commit count, pinned by short SHA + branch + commit date.
export interface VersionInfo {
  product: "baba";
  version: string;              // "0.1.68"
  base: string;                 // "0.1"
  count: number;                // commit count
  sha: string;                  // short commit hash
  branch: string;
  committed_at: string | null;  // ISO-8601
  dirty: boolean;               // uncommitted changes at stamp time
}

export interface SystemInfo {
  api_version: string;
  python_version: string;
  variant: string;
  gpu_device: string | null;
  embedder_model: string;
  face_models_present: boolean;
  detector_model: string;
  detector_family: string | null;
  plate_reading: boolean | null;
  auto_describe_enabled: boolean;
  cookie_secure: boolean;
  cors_origins: string[];
  postgres_sslmode: string;
}

export interface ServiceTiming {
  count: number;
  p50: number;
  p95: number;
  p99: number;
}

export interface ServiceMetricsHistoryPoint {
  ts_ns: number;
  rates: Record<string, number>;
  gauges: Record<string, number>;
  timings_p95: Record<string, number>;
}

export interface ServiceMetrics {
  service: string;
  instance: string;
  uptime_s: number;
  age_s: number;
  stale: boolean;
  rates: Record<string, number>;
  gauges: Record<string, number>;
  counters: Record<string, number>;
  timings: Record<string, ServiceTiming>;
  last_error: string | null;
  last_error_age_s: number | null;
  history: ServiceMetricsHistoryPoint[];
}

export interface SystemMetrics {
  generated_ts_ns: number;
  services: ServiceMetrics[];
}

export interface DetectionsMessage {
  camera_id: string;
  sequence: number;
  timestamp_ns: number;
  frame_width: number;
  frame_height: number;
  detections: {
    x1: number;
    y1: number;
    x2: number;
    y2: number;
    class_id: number;
    class_name: string;
    confidence: number;
    /** False = below the birth threshold but above the maintain floor: this
     *  box can keep an existing track alive, never start one. */
    birth_eligible?: boolean;
    /** Present when this box lands on a learned static-phantom spot the
     *  tracker is already suppressing — i.e. it goes no further than here.
     *  Carries the evidence so the overlay can say why. */
    phantom?: { births: number; span_h: number };
  }[];
}

export interface TracksMessage {
  camera_id: string;
  sequence: number;
  timestamp_ns: number;
  frame_width: number;
  frame_height: number;
  tracks: {
    track_id: number;
    x1: number;
    y1: number;
    x2: number;
    y2: number;
    class_id: number;
    class_name: string;
    confidence: number;
    /** "active" | "stationary" | "parked" — see tracker config. */
    motion_state: string;
    /** Wall-clock ns at which the current motion_state was entered. */
    state_since_ns: number;
  }[];
}

export interface IdentityAuditEvent {
  id: string;
  op: "merge" | "split" | "auto_match" | "ai_describe";
  at: string;
  payload: Record<string, unknown>;
}

/** One row of the event log as it is inserted; the details are fetched. */
export interface LiveEvent {
  id: string;
  camera_id: string;
  kind: string;
  at: string;
}

// --- scene-state regions (gate open/closed, door up/down, light on/off) ---
// A fixed polygon whose persistent state the state-evaluator classifies via
// DINOv2 few-shot prototypes. Orthogonal to zones (which test transient tracks).

export interface ScenePrototype {
  id: string;
  state_label: string;
  crop_path: string | null;
  captured_at: string;
}

export interface SceneRegionStatus {
  current_state: string | null;
  current_state_since: string | null;
  last_eval_at: string | null;
  last_label_raw: string | null;
  last_distance: number | null;
  unsure: boolean;
  unsure_since: string | null;
}

export interface SceneRegion {
  id: string;
  camera_id: string;
  name: string;
  place: string | null;
  polygon: [number, number][];
  states: string[];
  sample_interval_s: number;
  hysteresis_n: number;
  unknown_margin: number;
  color: string;
  enabled: boolean;
  created_at: string;
  updated_at: string;
  status: SceneRegionStatus | null;
  prototypes: ScenePrototype[];
}

export interface SceneRegionIn {
  name: string;
  /** Physical thing observed, when more than one camera can see it: two
   *  regions sharing a place are views of one spot. */
  place?: string | null;
  polygon: [number, number][];
  states?: string[];
  sample_interval_s?: number;
  hysteresis_n?: number;
  unknown_margin?: number;
  color?: string;
  enabled?: boolean;
}

export interface SceneRegionPatch {
  name?: string;
  place?: string | null;
  polygon?: [number, number][];
  states?: string[];
  sample_interval_s?: number;
  hysteresis_n?: number;
  unknown_margin?: number;
  color?: string;
  enabled?: boolean;
}

export interface SceneCaptureResult {
  prototype_id: string | null;
  crop_path: string | null;
  error: string | null;
}

export interface SceneEvalResult {
  state: string | null;
  raw_label: string | null;
  distance: number | null;
  per_state: Record<string, number>;
  error: string | null;
}

// --- telemetry incidents (pipeline flight recorder + AI verdicts) -----------

export interface IncidentChange {
  target: "camera_rule" | "camera";
  class_name: string | null;
  field: string;
  value: number;
  reason: string;
}

export interface TelemetryIncident {
  id: string;
  camera_slug: string;
  kind: string;
  opened_at: string;
  closed_at: string | null;
  status: "open" | "analyzed" | "applied" | "dismissed" | "closed" | "reverted";
  details: Record<string, unknown>;
  verdict: string | null;
  suggestion: {
    root_cause?: string;
    changes?: IncidentChange[];
    model?: string;
    latency_ms?: number;
    usage?: { input_tokens: number | null; output_tokens: number | null };
  } | null;
}

/** One detection as the gate saw it, normalised to the frame (0-1) so the
 * browser can draw it over a clip at any rendered size. `b` = birth-eligible:
 * it may start a NEW track, as opposed to merely keeping one alive. */
export interface ReplayBox {
  c: string;
  p: number;
  x: number;
  y: number;
  w: number;
  h: number;
  b: boolean;
}

/** What the suggested thresholds would have done, over the footage the
 * incident was opened on. `cur`/`new` are the SAME raw detections filtered by
 * the current and proposed rules — the frames are identical, only the gate
 * differs. */
export interface IncidentReplay {
  incident_id: string;
  camera_id: string;
  camera_slug: string;
  start_s: number;
  end_s: number;
  changes: IncidentChange[];
  /** Suggested fields that cannot be judged from boxes on a frame (tracker
   * knobs like park_seconds) — named so the picture never implies it covered
   * them. */
  not_shown: string[];
  frames: { t: number; cur: ReplayBox[]; new: ReplayBox[] }[];
  summary: {
    class_name: string;
    current_published: number;
    current_births: number;
    proposed_published: number;
    proposed_births: number;
  }[];
}

export interface IncidentAnalyzeResult {
  model: string;
  latency_ms: number;
  error: string | null;
  incident: TelemetryIncident | null;
}

export interface TelemetrySettings {
  auto_analyze: boolean;
}

export interface TelemetryUsage {
  analyses: number;
  input_tokens: number;
  output_tokens: number;
}

export interface ParkedVehicle {
  camera_id: string;
  place: string;
  name: string;
  since: string;
}
