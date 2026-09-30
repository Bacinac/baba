// Thin typed client for the BABA API. Browser always goes through SvelteKit
// (which proxies /api/* to the FastAPI service in dev, hits an env-configured
// origin in prod).

import { t } from "$lib/i18n";
import { ApiError, refusal, send, type Sending } from "$lib/kit";
import { liveFeed, type FeedHandlers } from "./live.svelte";
import type {
  AiDefaults, AiSettings, AiSettingsIn, AiTestResult,
  AuditEntry, AuditFacets, AuditFilter, AuditPurgeResult, BabaEvent,
  BabaUser, Camera, CameraDetectionRule, CameraIn,
  ClassifyPolygonResult, DeliveriesFilter, DeliveryRow, DetectionClass,
  DiscoverResult, EffectiveDetectionRule, EventsByCameraOut, EventsByHourOut, EventsByKindOut,
  FaceDetector, FaceModel, FaceRecognitionSettings,
  FaceRecognitionSettingsIn, FaceRecomputeStatus, GlobalDetectionRule, HeatmapClassGroup,
  HeatmapOut, IdentitiesFilter, IdentityAiDescribeResult, IdentityDetail,
  IdentityLabel, IdentityLabelIn, IdentityReferencePhoto, IdentitySummary,
  ImmichImportResult, ImmichPeopleResult, ImmichSettingsIn, ImmichStatus,
  OpusImportResult, OpusNewIdentityResult, OpusPeopleResult, OpusSettingsIn, OpusStatus,
  ReferenceSources,
  IncidentAnalyzeResult, IncidentReplay, NotificationChannel, NotificationChannelIn,
  NotificationRule, NotificationRuleIn, NotificationTestResult, ProbeResult,
  PurgeRecordingsResult, PurgeScope, Recording, RecordingSettings, RecordingSettingsIn,
  RecordingsFilter, ReferencePhotoUploadResult, SceneCaptureResult, SceneEvalResult,
  SceneRegion, SceneRegionIn, SceneRegionPatch, SearchResult,
  SegmentAtPointRequest, SegmentAtPointResult, Sighting, SightingsFilter,
  SuppressedTrack,
  Stats, StorageUsage, StreamPreset, Tunable, SuggestZonesResult, SystemInfo,
  VersionInfo, SystemMetrics, TelemetryIncident, TelemetrySettings,
  TelemetryUsage, LiveOverlay, LiveStatus, TestConnectionResult,
  TrackingDefaults, TuneZoneRulesRequest, TuneZoneRulesResult, UserPreferences,
  Zone, ZoneIn, ZonePatch,
  ParkedVehicle, ParkingEpisode, PresenceEpisode,
  DetectionsMessage, IdentityAuditEvent, LiveEvent, TracksMessage,
} from "./types";
export * from "./types";
export { live, type Feed } from "./live.svelte";

/** URL to a short, server-cut faststart clip for an absolute time window on a
 * camera — starts near instantly and spans segment boundaries, so a visit that
 * crosses a 60s segment plays as one continuous clip. `start`/`end` are epoch
 * seconds (UTC). */

export function cameraClipUrl(cameraId: string, start: number, end: number): string {
  return `${API}/recordings/cameras/${cameraId}/clip?start=${Math.round(start * 1000) / 1000}&end=${Math.round(end * 1000) / 1000}`;
}

/** Absolute URL for a thumbnail path the API returned. */
export function thumbnailUrl(rel: string): string {
  return `${API}/${rel}`;
}

export const IDENTITY_AUTO_MATCH_THRESHOLD = 0.35;


const API = "/api";

/** Every call to the api goes through here: the session cookie on the kit's
 *  transport. */
export function request(path: string, init: Sending = {}): Promise<Response> {
  return send(`${API}${path}`, { credentials: "include", ...init });
}

async function ok(r: Response): Promise<Response> {
  if (r.ok) return r;
  if (r.status === 401) throw new ApiError(401, t("api_unauthorized"));
  if (r.status === 403) throw new ApiError(403, t("api_forbidden"));
  throw await refusal(r);
}

async function jsonOrThrow(r: Response) {
  await ok(r);
  if (r.status === 204) return null;
  return r.json();
}

export const api = {
  listCameras: () =>
    request(`/cameras`).then(jsonOrThrow) as Promise<Camera[]>,
  getCamera: (id: string) =>
    request(`/cameras/${id}`).then(jsonOrThrow) as Promise<Camera>,
  createCamera: (body: CameraIn) =>
    request(`/cameras`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(jsonOrThrow) as Promise<Camera>,
  patchCamera: (id: string, body: Partial<CameraIn>) =>
    request(`/cameras/${id}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(jsonOrThrow) as Promise<Camera>,
  getLiveOverlay: () =>
    request(`/settings/live-overlay`).then(jsonOrThrow) as Promise<LiveOverlay>,
  putLiveOverlay: (body: LiveOverlay) =>
    request(`/settings/live-overlay`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(jsonOrThrow) as Promise<LiveOverlay>,
  getLiveStatus: () =>
    request(`/live/status`).then(jsonOrThrow) as Promise<
      Record<string, LiveStatus>
    >,
  getParkedVehicles: () =>
    request(`/live/parked`).then(jsonOrThrow) as Promise<ParkedVehicle[]>,
  getTrackingDefaults: () =>
    request(`/settings/tracking`).then(jsonOrThrow) as Promise<TrackingDefaults>,
  putTrackingDefaults: (body: TrackingDefaults) =>
    request(`/settings/tracking`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(jsonOrThrow) as Promise<TrackingDefaults>,
  renameCameraSlug: (id: string, slug: string) =>
    request(`/cameras/${id}/rename-slug`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ slug }),
    }).then(jsonOrThrow) as Promise<Camera>,
  deleteCamera: (id: string) =>
    request(`/cameras/${id}`, { method: "DELETE" }).then(jsonOrThrow),

  streamPresets: (): Promise<StreamPreset[]> =>
    request(`/stream-presets`).then(jsonOrThrow) as Promise<StreamPreset[]>,

  testConnection: (stream_url: string): Promise<TestConnectionResult> =>
    request(`/cameras/test-connection`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ stream_url }),
    }).then(jsonOrThrow) as Promise<TestConnectionResult>,

  discoverCameras: (subnet: string): Promise<DiscoverResult> =>
    request(`/cameras/discover`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ subnet }),
    }).then(jsonOrThrow) as Promise<DiscoverResult>,

  probeCamera: (ip: string, username: string, password: string): Promise<ProbeResult> =>
    request(`/cameras/probe`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ ip, username, password }),
    }).then(jsonOrThrow) as Promise<ProbeResult>,

  quickaddCamera: (body: { stream_url: string; substream_url?: string | null; ip?: string; name?: string }): Promise<Camera> =>
    request(`/cameras/quickadd`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(jsonOrThrow) as Promise<Camera>,

  // --- users (admin-only except self) ---

  listUsers: (): Promise<BabaUser[]> =>
    request(`/users`).then(jsonOrThrow) as Promise<BabaUser[]>,
  getUser: (id: string): Promise<BabaUser> =>
    request(`/users/${id}`).then(jsonOrThrow) as Promise<BabaUser>,

  createUser: (body: { username: string; password: string; role: string }): Promise<BabaUser> =>
    request(`/users`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(jsonOrThrow) as Promise<BabaUser>,

  patchUser: (id: string, body: { username?: string; role?: string }): Promise<BabaUser> =>
    request(`/users/${id}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(jsonOrThrow) as Promise<BabaUser>,

  deleteUser: (id: string) =>
    request(`/users/${id}`, { method: "DELETE" }).then(jsonOrThrow),

  resetUserPassword: (id: string, password: string) =>
    request(`/users/${id}/password`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ password }),
    }).then(jsonOrThrow),

  adminDisable2fa: (id: string): Promise<null> =>
    request(`/users/${id}/2fa/disable`, {
      method: "POST",
    }).then(jsonOrThrow),

  getPreferences: (): Promise<UserPreferences> =>
    request(`/auth/preferences`).then(jsonOrThrow) as Promise<UserPreferences>,

  patchPreferences: (body: UserPreferences): Promise<UserPreferences> =>
    request(`/auth/preferences`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(jsonOrThrow) as Promise<UserPreferences>,

  listSightings: (filter: SightingsFilter = {}): Promise<Sighting[]> => {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(filter)) {
      if (v !== undefined && v !== null && v !== "") qs.append(k, String(v));
    }
    const tail = qs.toString() ? `?${qs}` : "";
    return request(`/sightings${tail}`).then(jsonOrThrow) as Promise<Sighting[]>;
  },

  listSuppressedTracks: (filter: SightingsFilter = {}): Promise<SuppressedTrack[]> => {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(filter)) {
      if (v !== undefined && v !== null && v !== "") qs.append(k, String(v));
    }
    const tail = qs.toString() ? `?${qs}` : "";
    return request(`/tracks/suppressed${tail}`).then(
      jsonOrThrow,
    ) as Promise<SuppressedTrack[]>;
  },

  /** Merged recording coverage (runs, not 60 s segments) — the timeline's
   *  "footage exists here" layer. A continuous camera-day is ONE row. */
  recordingsCoverage: (filter: RecordingsFilter = {}): Promise<Recording[]> => {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(filter)) {
      if (v !== undefined && v !== null && v !== "") qs.append(k, String(v));
    }
    const tail = qs.toString() ? `?${qs}` : "";
    return request(`/recordings/coverage${tail}`).then(jsonOrThrow) as Promise<Recording[]>;
  },

  listIdentities: (filter: IdentitiesFilter = {}): Promise<IdentitySummary[]> => {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(filter)) {
      if (v !== undefined && v !== null && v !== "") qs.append(k, String(v));
    }
    const tail = qs.toString() ? `?${qs}` : "";
    return request(`/identities${tail}`).then(jsonOrThrow) as Promise<IdentitySummary[]>;
  },

  getIdentity: (globalId: string): Promise<IdentityDetail> =>
    request(`/identities/${globalId}`).then(jsonOrThrow) as Promise<IdentityDetail>,
  getIdentityPresence: (globalId: string, days = 7): Promise<PresenceEpisode[]> =>
    request(`/identities/${globalId}/presence?days=${days}`).then(jsonOrThrow) as Promise<PresenceEpisode[]>,

  /** Similarity grouping of unnamed identities — arrays of global_ids the
   *  gallery renders as one stack with one "Ovo je…" action. Only multi-member
   *  groups come back; absent gids are singles. */
  anonGroups: (): Promise<{ groups: string[][] }> =>
    request(`/identities/anon-groups`).then(jsonOrThrow) as Promise<{ groups: string[][] }>,

  /** Background purge of EVERY unnamed identity (202 + async — Cloudflare's
   *  ~100 s proxy limit forbids doing it inline). Labeled identities untouched. */
  deleteAllAnon: (): Promise<{ scheduled: number }> =>
    request(`/identities/anon`, { method: "DELETE" }).then(jsonOrThrow) as Promise<{ scheduled: number }>,

  upsertIdentityLabel: (globalId: string, body: IdentityLabelIn): Promise<IdentityLabel> =>
    request(`/identities/${globalId}/label`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(jsonOrThrow) as Promise<IdentityLabel>,

  deleteIdentityLabel: (globalId: string): Promise<null> =>
    request(`/identities/${globalId}/label`, {
      method: "DELETE",
    }).then(jsonOrThrow) as Promise<null>,

  /** Full purge: tracks, samples, events, label, photos, files.
   * Server unlinks JPEGs after the DB transaction commits. */
  deleteIdentity: (globalId: string): Promise<null> =>
    request(`/identities/${globalId}`, {
      method: "DELETE",
    }).then(jsonOrThrow) as Promise<null>,

  describeIdentity: (globalId: string): Promise<IdentityAiDescribeResult> =>
    request(`/identities/${globalId}/ai-describe`, {
      method: "POST",
    }).then(jsonOrThrow) as Promise<IdentityAiDescribeResult>,

  uploadReferencePhotos: (globalId: string, files: File[]): Promise<ReferencePhotoUploadResult> => {
    const form = new FormData();
    for (const f of files) form.append("files", f, f.name);
    return request(`/identities/${globalId}/reference-photos`, {
      method: "POST",
      body: form,
    }).then(jsonOrThrow) as Promise<ReferencePhotoUploadResult>;
  },

  referencePhotosFromTracks: (
    globalId: string, trackIds: string[],
  ): Promise<ReferencePhotoUploadResult> =>
    request(`/identities/${globalId}/reference-photos/from-tracks`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ track_ids: trackIds }),
    }).then(jsonOrThrow) as Promise<ReferencePhotoUploadResult>,

  referencePhotosAutoSelect: (
    globalId: string,
    body: { count?: number; min_observations?: number } = {},
  ): Promise<ReferencePhotoUploadResult> =>
    request(`/identities/${globalId}/reference-photos/auto-select`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(jsonOrThrow) as Promise<ReferencePhotoUploadResult>,

  referencePhotosFromFaceSamples: (
    globalId: string,
    body: { count?: number; min_face_score?: number; min_face_px?: number } = {},
  ): Promise<ReferencePhotoUploadResult> =>
    request(`/identities/${globalId}/reference-photos/from-face-samples`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(jsonOrThrow) as Promise<ReferencePhotoUploadResult>,

  setIdentityCoverPhoto: (
    globalId: string, photoPath: string | null,
  ): Promise<IdentityLabel> =>
    request(`/identities/${globalId}/cover-photo`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ photo_path: photoPath }),
    }).then(jsonOrThrow) as Promise<IdentityLabel>,

  listReferencePhotos: (globalId: string): Promise<IdentityReferencePhoto[]> =>
    request(`/identities/${globalId}/reference-photos`)
      .then(jsonOrThrow) as Promise<IdentityReferencePhoto[]>,

  /** Drop this identity's reference photos that carry no face — they buy no
   *  recognition (face is the only cross-session signal) but still inflate
   *  reference_count, so the enrollment reads stronger than it is. */
  deleteFacelessReferencePhotos: (
    globalId: string,
  ): Promise<{ removed: number; label: IdentityLabel | null }> =>
    request(`/identities/${globalId}/reference-photos/faceless`, {
      method: "DELETE",
    }).then(jsonOrThrow) as Promise<{ removed: number; label: IdentityLabel | null }>,

  deleteReferencePhoto: (globalId: string, photoId: string): Promise<null> =>
    request(`/identities/${globalId}/reference-photos/${photoId}`, {
      method: "DELETE",
    }).then(jsonOrThrow) as Promise<null>,

  referencePhotoUrl: (rel: string): string => `${API}/${rel}`,

  // --- external face reference sources ---

  /** Which libraries are configured, from settings alone (no ping). */
  referenceSources: (): Promise<ReferenceSources> =>
    request(`/reference-sources`).then(jsonOrThrow) as Promise<ReferenceSources>,

  // --- Immich (face reference import) ---

  immichStatus: (): Promise<ImmichStatus> =>
    request(`/immich/status`).then(jsonOrThrow) as Promise<ImmichStatus>,

  /** Saving IS testing — the server pings Immich with the key before storing
   *  it and 400s on failure, so there's no separate test call. */
  immichSetSettings: (body: ImmichSettingsIn): Promise<ImmichStatus> =>
    request(`/immich/settings`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(jsonOrThrow) as Promise<ImmichStatus>,

  immichPeople: (): Promise<ImmichPeopleResult> =>
    request(`/immich/people`).then(jsonOrThrow) as Promise<ImmichPeopleResult>,

  referencePhotosFromImmich: (
    globalId: string,
    body: { person_id: string; limit?: number },
  ): Promise<ImmichImportResult> =>
    request(`/identities/${globalId}/reference-photos/from-immich`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(jsonOrThrow) as Promise<ImmichImportResult>,

  // --- OPUS · Library (face reference import) ---

  opusStatus: (): Promise<OpusStatus> =>
    request(`/opus/status`).then(jsonOrThrow) as Promise<OpusStatus>,

  /** Saving IS testing — the server pings the library with the token before
   *  storing it and 400s on failure, so there's no separate test call. */
  opusSetSettings: (body: OpusSettingsIn): Promise<OpusStatus> =>
    request(`/opus/settings`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(jsonOrThrow) as Promise<OpusStatus>,

  opusPeople: (): Promise<OpusPeopleResult> =>
    request(`/opus/people`).then(jsonOrThrow) as Promise<OpusPeopleResult>,

  /** Proxied through the api: the browser holds no library token. */
  opusPersonCoverUrl: (personId: number): string => `${API}/opus/people/${personId}/cover`,

  referencePhotosFromOpus: (
    globalId: string,
    body: { person_id?: number | null; limit?: number },
  ): Promise<OpusImportResult> =>
    request(`/identities/${globalId}/reference-photos/from-opus`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(jsonOrThrow) as Promise<OpusImportResult>,

  identityFromOpus: (body: { person_id: number; limit?: number }): Promise<OpusNewIdentityResult> =>
    request(`/identities/from-opus`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(jsonOrThrow) as Promise<OpusNewIdentityResult>,

  mergeIdentity: (globalId: string, into: string): Promise<{ merged: number; into: string }> =>
    request(`/identities/${globalId}/merge`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ into }),
    }).then(jsonOrThrow) as Promise<{ merged: number; into: string }>,

  splitIdentity: (globalId: string, trackId: string): Promise<{ split_track_id: string; new_global_id: string }> =>
    request(`/identities/${globalId}/split`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ track_id: trackId }),
    }).then(jsonOrThrow) as Promise<{ split_track_id: string; new_global_id: string }>,

  /** Hard-delete sighting tracks (row + crop/thumbnail JPEGs). Used to prune
   *  redundant near-duplicate sightings that won't promote to references. */
  deleteSightings: (globalId: string, trackIds: string[]): Promise<{ deleted: number }> =>
    request(`/identities/${globalId}/delete-sightings`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ track_ids: trackIds }),
    }).then(jsonOrThrow) as Promise<{ deleted: number }>,

  // --- recording policy ---
  getRecordingSettings: (): Promise<RecordingSettings> =>
    request(`/recording-settings`).then(jsonOrThrow) as Promise<RecordingSettings>,

  putRecordingSettings: (body: RecordingSettingsIn): Promise<RecordingSettings> =>
    request(`/recording-settings`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(jsonOrThrow) as Promise<RecordingSettings>,

  /** Destructive, admin-only: delete every recording (DB rows + segment/clip
   *  files on disk). Events/tracks/identities are untouched. */
  purgeAllRecordings: (scope: PurgeScope = "recordings"): Promise<PurgeRecordingsResult> =>
    request(`/recordings?scope=${scope}`, { method: "DELETE" }).then(
      jsonOrThrow,
    ) as Promise<PurgeRecordingsResult>,

  // --- snapshot ---

  /** URL for a static JPEG of the camera's current frame. Bust the cache with
   * a query param when you want a fresh frame. */
  snapshotUrl: (cameraId: string, bust?: number | string): string => {
    const tail = bust ? `?t=${encodeURIComponent(String(bust))}` : "";
    return `${API}/cameras/${cameraId}/snapshot${tail}`;
  },

  // --- AI settings ---

  aiDefaults: (): Promise<AiDefaults> =>
    request(`/ai/defaults`).then(jsonOrThrow) as Promise<AiDefaults>,

  listAiSettings: (): Promise<AiSettings[]> =>
    request(`/ai/settings`).then(jsonOrThrow) as Promise<AiSettings[]>,

  saveAiSettings: (body: AiSettingsIn): Promise<AiSettings> =>
    request(`/ai/settings`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(jsonOrThrow) as Promise<AiSettings>,

  deleteAiSettings: (provider: string) =>
    request(`/ai/settings/${provider}`, { method: "DELETE" }).then(jsonOrThrow),

  // --- Face recognition ---

  listFaceModels: (): Promise<FaceModel[]> =>
    request(`/face-recognition/models`).then(jsonOrThrow) as Promise<FaceModel[]>,

  listFaceDetectors: (): Promise<FaceDetector[]> =>
    request(`/face-recognition/detectors`).then(jsonOrThrow) as Promise<FaceDetector[]>,

  getFaceRecognitionSettings: (): Promise<FaceRecognitionSettings> =>
    request(`/face-recognition/settings`).then(jsonOrThrow) as Promise<FaceRecognitionSettings>,

  saveFaceRecognitionSettings: (body: FaceRecognitionSettingsIn): Promise<FaceRecognitionSettings> =>
    request(`/face-recognition/settings`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(jsonOrThrow) as Promise<FaceRecognitionSettings>,

  startFaceRecompute: (): Promise<{ job_id: string }> =>
    request(`/face-recognition/recompute`, {
      method: "POST",
    }).then(jsonOrThrow) as Promise<{ job_id: string }>,

  getFaceRecomputeStatus: (jobId: string): Promise<FaceRecomputeStatus> =>
    request(`/face-recognition/recompute/${jobId}`).then(jsonOrThrow) as Promise<FaceRecomputeStatus>,

  testAiCredentials: (body: { provider?: string; api_key?: string; model?: string }): Promise<AiTestResult> =>
    request(`/ai/test`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(jsonOrThrow) as Promise<AiTestResult>,

  suggestZones: (cameraId: string, locale: string, provider?: string): Promise<SuggestZonesResult> => {
    const qs = new URLSearchParams({ locale });
    if (provider) qs.set("provider", provider);
    return request(`/ai/suggest-zones/${cameraId}?${qs}`, {
      method: "POST",
    }).then(jsonOrThrow) as Promise<SuggestZonesResult>;
  },

  tuneZoneRules: (
    cameraId: string,
    body: TuneZoneRulesRequest,
    provider?: string,
  ): Promise<TuneZoneRulesResult> => {
    const qs = new URLSearchParams();
    if (provider) qs.set("provider", provider);
    return request(`/ai/tune-zone-rules/${cameraId}${qs.toString() ? `?${qs}` : ""}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(jsonOrThrow) as Promise<TuneZoneRulesResult>;
  },

  segmentAtPoint: (cameraId: string, body: SegmentAtPointRequest): Promise<SegmentAtPointResult> =>
    request(`/ai/segment-at-point/${cameraId}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(jsonOrThrow) as Promise<SegmentAtPointResult>,

  classifyPolygon: (
    cameraId: string,
    polygon: [number, number][],
    locale: string,
    provider?: string,
  ): Promise<ClassifyPolygonResult> =>
    request(`/ai/classify-polygon/${cameraId}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ polygon, locale, provider }),
    }).then(jsonOrThrow) as Promise<ClassifyPolygonResult>,

  // --- zones ---

  listZones: (cameraId: string): Promise<Zone[]> =>
    request(`/cameras/${cameraId}/zones`).then(jsonOrThrow) as Promise<Zone[]>,

  createZone: (cameraId: string, body: ZoneIn): Promise<Zone> =>
    request(`/cameras/${cameraId}/zones`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(jsonOrThrow) as Promise<Zone>,

  patchZone: (zoneId: string, body: ZonePatch): Promise<Zone> =>
    request(`/zones/${zoneId}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(jsonOrThrow) as Promise<Zone>,

  deleteZone: (zoneId: string) =>
    request(`/zones/${zoneId}`, { method: "DELETE" }).then(jsonOrThrow),

  // --- detection rules (three-layer sensitivity / class filter) ---

  listDetectionClasses: (): Promise<DetectionClass[]> =>
    request(`/detection-rules/classes`).then(jsonOrThrow) as Promise<DetectionClass[]>,

  listGlobalDetectionRules: (): Promise<GlobalDetectionRule[]> =>
    request(`/detection-rules/global`).then(jsonOrThrow) as Promise<GlobalDetectionRule[]>,

  putGlobalDetectionRule: (
    className: string,
    body: { min_confidence: number; enabled: boolean; min_box_pct?: number },
  ): Promise<GlobalDetectionRule> =>
    request(`/detection-rules/global/${encodeURIComponent(className)}`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(jsonOrThrow) as Promise<GlobalDetectionRule>,

  deleteGlobalDetectionRule: (className: string) =>
    request(`/detection-rules/global/${encodeURIComponent(className)}`, {
      method: "DELETE",
    }).then(jsonOrThrow),

  listTunables: (): Promise<Tunable[]> =>
    request(`/tunables`).then(jsonOrThrow) as Promise<Tunable[]>,

  /** Partial update; `null` clears a value and returns it to the deployment default. */
  putTunables: (values: Record<string, number | null>): Promise<Tunable[]> =>
    request(`/tunables`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ values }),
    }).then(jsonOrThrow) as Promise<Tunable[]>,

  listCameraDetectionRules: (cameraId: string): Promise<CameraDetectionRule[]> =>
    request(`/cameras/${cameraId}/detection-rules`)
      .then(jsonOrThrow) as Promise<CameraDetectionRule[]>,

  /** What this camera ACTUALLY applies per class, resolved by the same code
   *  the detector runs. Never derive this client-side — the fallbacks are
   *  where the two answers drift. */
  listCameraDetectionRulesEffective: (cameraId: string): Promise<EffectiveDetectionRule[]> =>
    request(`/cameras/${cameraId}/detection-rules/effective`)
      .then(jsonOrThrow) as Promise<EffectiveDetectionRule[]>,

  putCameraDetectionRule: (
    cameraId: string, className: string,
    body: {
      min_confidence?: number | null;
      enabled?: boolean | null;
      min_box_pct?: number | null;
    },
  ): Promise<CameraDetectionRule> =>
    request(`/cameras/${cameraId}/detection-rules/${encodeURIComponent(className)}`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(jsonOrThrow) as Promise<CameraDetectionRule>,

  deleteCameraDetectionRule: (cameraId: string, className: string) =>
    request(`/cameras/${cameraId}/detection-rules/${encodeURIComponent(className)}`, {
      method: "DELETE",
    }).then(jsonOrThrow),

  // --- stats / system / storage (Dashboard, Settings → System, Settings → Storage) ---

  // --- 2FA TOTP ---

  totpStatus: (): Promise<{ enabled: boolean; enrolling: boolean }> =>
    request(`/auth/2fa/status`).then(jsonOrThrow) as Promise<{ enabled: boolean; enrolling: boolean }>,

  totpSetup: (): Promise<{ secret: string; otpauth_uri: string }> =>
    request(`/auth/2fa/setup`, { method: "POST" })
      .then(jsonOrThrow) as Promise<{ secret: string; otpauth_uri: string }>,

  totpEnable: (code: string): Promise<{ codes: string[] }> =>
    request(`/auth/2fa/enable`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ code }),
    }).then(jsonOrThrow) as Promise<{ codes: string[] }>,

  totpRegenerateRecovery: (password: string): Promise<{ codes: string[] }> =>
    request(`/auth/2fa/recovery-codes/regenerate`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ password }),
    }).then(jsonOrThrow) as Promise<{ codes: string[] }>,

  totpDisable: (password: string): Promise<null> =>
    request(`/auth/2fa/disable`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ password }),
    }).then(jsonOrThrow),

  totpQrUrl: (): string => `${API}/auth/2fa/qr`,

  getStats: (): Promise<Stats> =>
    request(`/stats`).then(jsonOrThrow) as Promise<Stats>,

  getStorageUsage: (): Promise<StorageUsage> =>
    request(`/storage/usage`).then(jsonOrThrow) as Promise<StorageUsage>,

  getSystemInfo: (): Promise<SystemInfo> =>
    request(`/system/info`).then(jsonOrThrow) as Promise<SystemInfo>,

  getVersion: (): Promise<VersionInfo> =>
    request(`/version`).then(jsonOrThrow) as Promise<VersionInfo>,

  getSystemMetrics: (): Promise<SystemMetrics> =>
    request(`/system/metrics`).then(jsonOrThrow) as Promise<SystemMetrics>,

  // --- notification channels (Settings → Notifications) ---

  listNotificationChannels: (): Promise<NotificationChannel[]> =>
    request(`/notifications/channels`).then(jsonOrThrow) as Promise<NotificationChannel[]>,

  createNotificationChannel: (body: NotificationChannelIn): Promise<NotificationChannel> =>
    request(`/notifications/channels`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(jsonOrThrow) as Promise<NotificationChannel>,

  patchNotificationChannel: (
    id: string, body: Partial<NotificationChannelIn>,
  ): Promise<NotificationChannel> =>
    request(`/notifications/channels/${id}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(jsonOrThrow) as Promise<NotificationChannel>,

  deleteNotificationChannel: (id: string): Promise<null> =>
    request(`/notifications/channels/${id}`, {
      method: "DELETE",
    }).then(jsonOrThrow),

  testNotificationChannel: (id: string): Promise<NotificationTestResult> =>
    request(`/notifications/channels/${id}/test`, {
      method: "POST",
    }).then(jsonOrThrow) as Promise<NotificationTestResult>,

  // --- notification rules (bind events to channels) ---

  listNotificationRules: (): Promise<NotificationRule[]> =>
    request(`/notifications/rules`).then(jsonOrThrow) as Promise<NotificationRule[]>,

  createNotificationRule: (body: NotificationRuleIn): Promise<NotificationRule> =>
    request(`/notifications/rules`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(jsonOrThrow) as Promise<NotificationRule>,

  patchNotificationRule: (
    id: string, body: Partial<NotificationRuleIn>,
  ): Promise<NotificationRule> =>
    request(`/notifications/rules/${id}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(jsonOrThrow) as Promise<NotificationRule>,

  deleteNotificationRule: (id: string): Promise<null> =>
    request(`/notifications/rules/${id}`, {
      method: "DELETE",
    }).then(jsonOrThrow),

  listNotificationDeliveries: (filter: DeliveriesFilter = {}): Promise<DeliveryRow[]> => {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(filter)) {
      if (v !== undefined && v !== null && v !== "") qs.append(k, String(v));
    }
    const tail = qs.toString() ? `?${qs}` : "";
    return request(`/notifications/deliveries${tail}`).then(jsonOrThrow) as Promise<DeliveryRow[]>;
  },

  // --- analytics (Analytics page) ---

  getEventsByHour: (
    opts: { days?: number; kind?: string; camera_id?: string } = {},
  ): Promise<EventsByHourOut> => {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(opts)) {
      if (v !== undefined && v !== null && v !== "") qs.append(k, String(v));
    }
    const tail = qs.toString() ? `?${qs}` : "";
    return request(`/analytics/events_by_hour${tail}`).then(jsonOrThrow) as Promise<EventsByHourOut>;
  },

  getEventsByCamera: (days = 7): Promise<EventsByCameraOut> =>
    request(`/analytics/events_by_camera?days=${days}`).then(jsonOrThrow) as Promise<EventsByCameraOut>,

  getEventsByKind: (days = 7): Promise<EventsByKindOut> =>
    request(`/analytics/events_by_kind?days=${days}`).then(jsonOrThrow) as Promise<EventsByKindOut>,
  getParkingHistory: (days = 7): Promise<ParkingEpisode[]> =>
    request(`/analytics/parking?days=${days}`).then(jsonOrThrow) as Promise<ParkingEpisode[]>,

  getHeatmap: (
    cameraId: string, days = 7, classGroup: HeatmapClassGroup = "all",
  ): Promise<HeatmapOut> => {
    const qs = new URLSearchParams({ days: String(days), class_group: classGroup });
    return request(`/analytics/heatmap/${cameraId}?${qs}`).then(jsonOrThrow) as Promise<HeatmapOut>;
  },

  listAudit: (filter: AuditFilter = {}): Promise<AuditEntry[]> => {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(filter)) {
      if (v !== undefined && v !== null && v !== "") qs.append(k, String(v));
    }
    const tail = qs.toString() ? `?${qs}` : "";
    return request(`/audit${tail}`).then(jsonOrThrow) as Promise<AuditEntry[]>;
  },

  auditFacets: (): Promise<AuditFacets> =>
    request("/audit/facets").then(jsonOrThrow) as Promise<AuditFacets>,

  purgeAudit: (olderThanDays: number): Promise<AuditPurgeResult> => {
    const qs = new URLSearchParams({ older_than_days: String(olderThanDays) });
    return request(`/audit?${qs}`, {
      method: "DELETE",
    }).then(jsonOrThrow) as Promise<AuditPurgeResult>;
  },

  searchVisual: (
    file: File,
    opts: { limit?: number; class_id?: number; camera_id?: string; max_distance?: number } = {},
  ): Promise<SearchResult> => {
    const fd = new FormData();
    fd.append("file", file);
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(opts)) {
      if (v !== undefined && v !== null && v !== "") qs.append(k, String(v));
    }
    const tail = qs.toString() ? `?${qs}` : "";
    return request(`/search/visual${tail}`, {
      method: "POST",
      body: fd,
    }).then(jsonOrThrow) as Promise<SearchResult>;
  },

  // --- scene-state regions (gate open/closed, …) ---
  listSceneRegions: (cameraId: string): Promise<SceneRegion[]> =>
    request(`/cameras/${cameraId}/scene-regions`).then(
      jsonOrThrow,
    ) as Promise<SceneRegion[]>,

  createSceneRegion: (cameraId: string, body: SceneRegionIn): Promise<SceneRegion> =>
    request(`/cameras/${cameraId}/scene-regions`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(jsonOrThrow) as Promise<SceneRegion>,

  patchSceneRegion: (regionId: string, body: SceneRegionPatch): Promise<SceneRegion> =>
    request(`/scene-regions/${regionId}`, {
      method: "PATCH",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(jsonOrThrow) as Promise<SceneRegion>,

  deleteSceneRegion: (regionId: string) =>
    request(`/scene-regions/${regionId}`, { method: "DELETE" }).then(
      jsonOrThrow,
    ),

  // `at` (ISO, UTC) takes the reference from the recording covering that
  // moment instead of the live frame; omit it to capture what the camera sees
  // right now.
  captureScenePrototype: (
    regionId: string,
    stateLabel: string,
    at?: string,
  ): Promise<SceneCaptureResult> =>
    request(`/scene-regions/${regionId}/capture`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ state_label: stateLabel, at: at ?? null }),
    }).then(jsonOrThrow) as Promise<SceneCaptureResult>,

  evaluateSceneRegion: (regionId: string): Promise<SceneEvalResult> =>
    request(`/scene-regions/${regionId}/evaluate`).then(
      jsonOrThrow,
    ) as Promise<SceneEvalResult>,

  deleteScenePrototype: (prototypeId: string) =>
    request(`/scene-prototypes/${prototypeId}`, { method: "DELETE" }).then(
      jsonOrThrow,
    ),

  // --- telemetry incidents --------------------------------------------------

  listIncidents: (includeClosed = true): Promise<TelemetryIncident[]> =>
    request(`/telemetry/incidents?include_closed=${includeClosed}`).then(
      jsonOrThrow,
    ) as Promise<TelemetryIncident[]>,

  analyzeIncident: (id: string): Promise<IncidentAnalyzeResult> =>
    request(`/telemetry/incidents/${id}/analyze`, { method: "POST" }).then(
      jsonOrThrow,
    ) as Promise<IncidentAnalyzeResult>,

  applyIncident: (id: string): Promise<TelemetryIncident> =>
    request(`/telemetry/incidents/${id}/apply`, { method: "POST" }).then(
      jsonOrThrow,
    ) as Promise<TelemetryIncident>,

  dismissIncident: (id: string): Promise<TelemetryIncident> =>
    request(`/telemetry/incidents/${id}/dismiss`, { method: "POST" }).then(
      jsonOrThrow,
    ) as Promise<TelemetryIncident>,

  revertIncident: (id: string): Promise<TelemetryIncident> =>
    request(`/telemetry/incidents/${id}/revert`, { method: "POST" }).then(
      jsonOrThrow,
    ) as Promise<TelemetryIncident>,

  /** Replay the incident window under both the current and the suggested
   * thresholds. Read-only — it changes nothing, it only shows what the change
   * would do. Computed once and stored on the incident, because the detector's
   * trace ring rolls and an incident is usually reviewed later. */
  replayIncident: (id: string): Promise<IncidentReplay> =>
    request(`/telemetry/incidents/${id}/replay`, { method: "POST" }).then(
      jsonOrThrow,
    ) as Promise<IncidentReplay>,

  getTelemetrySettings: (): Promise<TelemetrySettings> =>
    request(`/telemetry/settings`).then(jsonOrThrow) as Promise<TelemetrySettings>,

  getTelemetryUsage: (): Promise<TelemetryUsage> =>
    request(`/telemetry/usage`).then(jsonOrThrow) as Promise<TelemetryUsage>,

  putTelemetrySettings: (body: TelemetrySettings): Promise<TelemetrySettings> =>
    request(`/telemetry/settings`, {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    }).then(jsonOrThrow) as Promise<TelemetrySettings>,
};

// --- visual search types ---

const cameraQuery = (slug?: string) => (slug ? `?camera=${encodeURIComponent(slug)}` : "");

/** Every box the detector drew, frame by frame. */
export const detectionsFeed = (slug: string | undefined, on: FeedHandlers<DetectionsMessage>) =>
  liveFeed(`/api/sse/detections${cameraQuery(slug)}`, on);

/** Live tracker output: same cadence as detections but with track_id +
 * motion_state per box. Used by the zone editor to render PARKED badges
 * and by anywhere else that needs "is this the same object as the
 * previous frame" without re-implementing IoU matching client-side. */
export const tracksFeed = (slug: string | undefined, on: FeedHandlers<TracksMessage>) =>
  liveFeed(`/api/sse/tracks${cameraQuery(slug)}`, on);

/** One message per event insert; the details are refetched. */
export const eventsFeed = (on: FeedHandlers<LiveEvent>) => liveFeed(`/api/sse/events`, on);

/** One message per `identity_audit` insert, so the identities list and
 *  detail refetch instead of polling. */
export const identitiesFeed = (on: FeedHandlers<IdentityAuditEvent>) =>
  liveFeed(`/api/sse/identities`, on);
