import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const ts: typeof import("typescript") = createRequire(import.meta.url)("typescript");

function source(file: string) {
  const text = readFileSync(new URL(file, import.meta.url), "utf8");
  const script = text.match(/<script lang="ts">([\s\S]*?)<\/script>/)![1];
  return ts.createSourceFile(file, script, ts.ScriptTarget.Latest, true);
}

function evaluate<T>(file: string, names: string[], scope: object): T {
  const tree = source(file);
  const functions = tree.statements.filter((node) =>
    ts.isFunctionDeclaration(node) && names.includes(node.name?.text ?? ""),
  );
  expect(functions).toHaveLength(names.length);
  const code = ts.transpileModule(functions.map((node) => node.getText(tree)).join("\n"), {
    compilerOptions: { target: ts.ScriptTarget.ES2022 },
  }).outputText;
  return new Function("scope", `with (scope) { ${code}; return { ${names.join(",")} }; }`)(scope);
}

function lifecycle(file: string, needle: string, scope: object) {
  const tree = source(file);
  const statement = tree.statements.find((node) =>
    ts.isExpressionStatement(node) && node.getText(tree).includes(needle),
  );
  expect(statement).toBeDefined();
  const code = ts.transpileModule(statement!.getText(tree), {
    compilerOptions: { target: ts.ScriptTarget.ES2022 },
  }).outputText;
  new Function("scope", `with (scope) { ${code} }`)(scope);
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); });

const cameraFile = "../routes/settings/cameras/[id]/+page.svelte";
const camera = (id: string) => ({ id, name: `Camera ${id}`, slug: id.toLowerCase() });
type CameraActions = Record<string, (...args: unknown[]) => Promise<void>>;

function cameraFixture() {
  const pending = deferred<ReturnType<typeof camera>>();
  const patchCamera = vi.fn().mockImplementationOnce(() => pending.promise)
    .mockImplementation(async (id, edit) => ({ ...camera(id), ...edit }));
  const scope = {
    id: "A", cam: camera("A"), edit: { name: "Camera A" }, loadSeq: 0,
    disposed: false, loading: false, loadError: null, saving: false,
    saveError: null, savedAt: null, renaming: false, renameSaving: false,
    renameDraft: "a-renamed", renameError: null, stillnessSaving: false,
    trackingDrag: {}, maintainDrag: null,
    api: { getCamera: vi.fn(async (id: string) => camera(id)), patchCamera,
      renameCameraSlug: patchCamera, deleteCamera: vi.fn() },
    dialog: { confirm: vi.fn(async () => true), alert: vi.fn() },
    t: (key: string) => key, goto: vi.fn(),
    get cameraBusy() { return this.saving || this.renameSaving || this.stillnessSaving; },
  };
  const actions = evaluate<CameraActions>(cameraFile,
    ["mutationTarget", "load", "save", "renameSlug", "saveMaintain", "saveTracking", "remove"], scope);
  return { scope, actions, pending, patchCamera };
}

describe("camera mutations", () => {
  it.each([
    ["save", { preventDefault() {} }],
    ["renameSlug", undefined],
    ["saveMaintain", 0.3],
    ["saveTracking", { park_seconds: 90 }],
  ])("keeps camera B selected when camera A's %s finishes late", async (method, arg) => {
    const { scope, actions, pending, patchCamera } = cameraFixture();
    const save = actions[method as string](arg);
    scope.id = "B";
    await actions.load();
    pending.resolve(camera("A"));
    await save;
    expect(scope.cam.id).toBe("B");
    expect(scope.savedAt).toBeNull();
    expect(scope.cameraBusy).toBe(false);
    await actions.save({ preventDefault() {} });
    expect(patchCamera).toHaveBeenLastCalledWith("B", expect.objectContaining({ name: "Camera B" }));
  });

  it("blocks an overlapping mutation of the same camera", async () => {
    const { actions, pending, patchCamera } = cameraFixture();
    const first = actions.saveMaintain(0.2);
    await actions.saveTracking({ park_seconds: 90 });
    expect(patchCamera).toHaveBeenCalledTimes(1);
    pending.resolve(camera("A"));
    await first;
    await actions.saveTracking({ park_seconds: 90 });
    expect(patchCamera).toHaveBeenCalledTimes(2);
  });

  it("does not delete a newly selected camera after confirmation of another camera", async () => {
    const { scope, actions } = cameraFixture();
    const answer = deferred<boolean>();
    scope.dialog.confirm.mockImplementation(() => answer.promise);
    const removal = actions.remove();
    scope.id = "B";
    await actions.load();
    answer.resolve(true);
    await removal;
    expect(scope.api.deleteCamera).not.toHaveBeenCalled();
  });
});

describe("zone rules", () => {
  it("locks a full-map save and preserves its cooldown in the next edit", async () => {
    const pending = deferred<object>();
    const initial = { id: "Z", rules: { enabled_classes: { person: { min_confidence: 0.5, cooldown_s: 10 } } } };
    const patchZone = vi.fn().mockImplementationOnce(() => pending.promise)
      .mockImplementation(async (id, patch) => ({ id, ...patch }));
    const scope = {
      zone: initial, zoneProp: initial, saving: false, error: null,
      api: { patchZone }, onUpdate: vi.fn(),
      get enabledClasses() { return this.zone.rules.enabled_classes; },
    };
    const actions = evaluate<CameraActions>("./ZoneRulesCard.svelte", ["commitRules", "setClassRule"], scope);
    const first = actions.setClassRule("person", { cooldown_s: 20 });
    await actions.setClassRule("person", { min_confidence: 0.7 });
    expect(patchZone).toHaveBeenCalledTimes(1);
    pending.resolve({ ...initial, rules: patchZone.mock.calls[0][1].rules });
    await first;
    await actions.setClassRule("person", { min_confidence: 0.7 });
    expect(patchZone).toHaveBeenLastCalledWith("Z", {
      rules: { enabled_classes: { person: { min_confidence: 0.7, cooldown_s: 20 } } },
    });
  });

  it("offers camera-enabled classes from the canonical effective rules", async () => {
    const rules = [
      { class_name: "dog", enabled: true, source: "override" },
      { class_name: "person", enabled: false, source: "override" },
    ];
    const scope = {
      zoneProp: { camera_id: "camera-C" }, effectiveClasses: [], error: null,
      api: { listCameraDetectionRulesEffective: vi.fn(async () => rules) },
      $effect: (fn: () => unknown) => fn(),
    };
    lifecycle("./ZoneRulesCard.svelte", "listCameraDetectionRulesEffective", scope);
    await Promise.resolve();
    expect(scope.api.listCameraDetectionRulesEffective).toHaveBeenCalledWith("camera-C");
    expect(scope.effectiveClasses).toEqual(["dog"]);
  });
});

describe("page subscription lifetime", () => {
  it.each([false, true])("does not open old-camera feeds after pending zones finish (failure=%s)", async (fails) => {
    const zones = deferred<never[]>();
    const entered = deferred<void>();
    const scope = {
      slug: "a", camera: null, loadError: null, lastMessage: null, lastTracksMsg: null,
      measuredFps: null, fpsArrivals: [], zones: [], es: null, esTracks: null,
      noteArrival: vi.fn(), t: (key: string) => key, cleanup: () => {},
      api: {
        listCameras: async () => [camera("A")], getCamera: async () => camera("A"),
        listZones: () => { entered.resolve(); return zones.promise; },
      },
      detectionsFeed: vi.fn(), tracksFeed: vi.fn(),
      $effect(fn: () => () => void) { this.cleanup = fn(); },
    };
    lifecycle("../routes/live/[slug]/+page.svelte", "const s = slug", scope);
    await entered.promise;
    scope.cleanup();
    if (fails) zones.reject(new Error("offline"));
    else zones.resolve([]);
    await Promise.resolve();
    await Promise.resolve();
    expect(scope.detectionsFeed).not.toHaveBeenCalled();
    expect(scope.tracksFeed).not.toHaveBeenCalled();
  });

  it("does not create an activity feed after its initial camera request outlives the page", async () => {
    const cameras = deferred<never[]>();
    const scope = {
      cameras: [], feed: null, liveTimer: null, cameraFilter: "", cleanup: () => {},
      api: { listCameras: () => cameras.promise }, eventsFeed: vi.fn(), scheduleLiveRefresh: vi.fn(),
      onMount(fn: () => () => void) { this.cleanup = fn(); },
    };
    lifecycle("../routes/activity/+page.svelte", "listCameras", scope);
    scope.cleanup();
    cameras.resolve([]);
    await Promise.resolve();
    expect(scope.eventsFeed).not.toHaveBeenCalled();
  });
});

describe("visual search invalidation", () => {
  it.each(["clear", "oversized file"])("does not restore pending results after %s", async (action) => {
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
    const pending = deferred<object>();
    const scope = {
      queryFile: { size: 1 }, queryPreview: "blob:old", searchSeq: 0, loading: false,
      result: null, error: null, fileInput: { value: "old" }, MAX_FILE_BYTES: 8,
      CLASS_GROUPS: [], classGroupFilter: "", cameraFilter: "", limit: 24, threshold: 0.55,
      api: { searchVisual: () => pending.promise }, ApiError: Error, t: (key: string) => key,
    };
    const actions = evaluate<Record<string, (...args: unknown[]) => unknown>>("../routes/search/+page.svelte",
      ["invalidateSearch", "runSearch", "clearAll", "pickFile"], scope);
    const search = actions.runSearch();
    if (action === "clear") actions.clearAll();
    else actions.pickFile({ size: 9 });
    expect(scope.loading).toBe(false);
    pending.resolve({ hits: [{ global_id: "old" }] });
    await search;
    expect(scope.result).toBeNull();
    expect(scope.queryFile).toBeNull();
    expect(scope.error).toBe(action === "clear" ? null : "search_error_too_big");
  });
});

function faceSettings(status: "active" | "pending" | "error", model = "new", jobId: string | null = null) {
  return {
    model_key: model, detector_key: "yunet", match_threshold: 0.4,
    active_model_key: status === "active" ? model : "old", active_detector_key: "yunet",
    activation_status: status, activation_error: status === "error" ? "Model load failed" : null,
    recompute_job_id: jobId,
  };
}

function faceFixture() {
  const scope = {
    settings: faceSettings("active", "old"), settingsSeq: 0, disposed: false,
    modelKey: "new", detectorKey: "yunet", threshold: 0.4,
    models: [{ key: "new", bundled_with_baba: true, file_present: true }],
    detectors: [{ key: "yunet", bundled_with_baba: true, file_present: true }],
    loading: false, saving: false, error: null, activeJob: null, startingRecompute: false,
    activationPollError: null, recomputePollError: null,
    attachedJobId: null as string | null, jobSeq: 0, activationPollSeq: 0, pollTimer: null, activationTimer: null,
    api: {
      saveFaceRecognitionSettings: vi.fn(async () => faceSettings("pending")),
      getFaceRecognitionSettings: vi.fn(async () => faceSettings("pending")),
      startFaceRecompute: vi.fn(async () => ({ job_id: "job-1" })),
      getFaceRecomputeStatus: vi.fn(async () => ({ job_id: "job-1", status: "running" })),
      listFaceModels: vi.fn(async () => []), listFaceDetectors: vi.fn(async () => []),
    },
    t: (key: string) => key,
  };
  const actions = evaluate<CameraActions>("./FaceRecognitionCard.svelte", [
    "activationReady", "recomputeBusy", "scheduleActivation", "attachRecompute", "pollActivation", "refresh",
    "selectedModel", "selectedDetector", "save", "pollRecompute", "startRecompute",
  ], scope);
  return { scope, actions };
}

describe("face model activation", () => {
  it.each(["activation", "recompute"])("clears a recovered %s poll error while preserving validation feedback", async (kind) => {
    const { scope, actions } = faceFixture();
    const feedback = scope as unknown as { error: string; activationPollError: string | null; recomputePollError: string | null };
    if (kind === "activation") scope.api.getFaceRecognitionSettings.mockResolvedValue(faceSettings("active"));
    else scope.attachedJobId = "job-1";
    feedback.error = "Model weights missing";
    const poll = kind === "activation"
      ? () => actions.pollActivation()
      : () => actions.pollRecompute("job-1");
    const request = kind === "activation"
      ? scope.api.getFaceRecognitionSettings
      : scope.api.getFaceRecomputeStatus;
    request.mockRejectedValueOnce(new Error("offline"));
    await poll();
    expect(kind === "activation" ? feedback.activationPollError : feedback.recomputePollError).toBe("offline");
    expect(feedback.error).toBe("Model weights missing");
    await vi.advanceTimersByTimeAsync(1500);
    expect(feedback.activationPollError).toBeNull();
    expect(feedback.recomputePollError).toBeNull();
    expect(feedback.error).toBe("Model weights missing");
  });

  it("attaches the server job after save and never submits an automatic recompute", async () => {
    const { scope, actions } = faceFixture();
    scope.api.saveFaceRecognitionSettings.mockResolvedValue(faceSettings("pending", "new", "job-1"));
    scope.api.getFaceRecognitionSettings.mockResolvedValue(faceSettings("pending", "new", "job-1"));
    scope.api.getFaceRecomputeStatus.mockResolvedValue({ job_id: "job-1", status: "pending" });
    await actions.save();
    expect(scope.activeJob).toEqual({ job_id: "job-1", status: "pending" });
    await actions.startRecompute();
    expect(scope.api.saveFaceRecognitionSettings).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1500);
    scope.api.getFaceRecognitionSettings.mockResolvedValue(faceSettings("active", "new", "job-1"));
    scope.api.getFaceRecomputeStatus.mockResolvedValue({ job_id: "job-1", status: "running" });
    await vi.advanceTimersByTimeAsync(1500);
    expect(scope.activeJob).toEqual({ job_id: "job-1", status: "running" });
    expect(scope.api.startFaceRecompute).not.toHaveBeenCalled();
    expect(scope.activationTimer).not.toBeNull();
  });

  it("resumes a persisted job on mount and stops polling once it finishes", async () => {
    const { scope, actions } = faceFixture();
    scope.api.getFaceRecognitionSettings.mockResolvedValue(faceSettings("active", "new", "job-1"));
    scope.api.getFaceRecomputeStatus.mockResolvedValueOnce({ job_id: "job-1", status: "pending" });
    await actions.refresh();
    expect(actions.recomputeBusy()).toBe(true);
    scope.api.getFaceRecomputeStatus.mockResolvedValue({ job_id: "job-1", status: "done" });
    await vi.advanceTimersByTimeAsync(1500);
    expect(actions.recomputeBusy()).toBe(false);
    expect(scope.activeJob).toEqual({ job_id: "job-1", status: "done" });
    expect(scope.pollTimer).toBeNull();
    expect(scope.activationTimer).toBeNull();
    expect(scope.api.startFaceRecompute).not.toHaveBeenCalled();
  });

  it("requires the active detector to match as well as the active model", async () => {
    const { scope, actions } = faceFixture();
    scope.settings = {
      ...faceSettings("active"), active_detector_key: "other-detector",
    };
    await actions.startRecompute();
    expect(scope.api.startFaceRecompute).not.toHaveBeenCalled();
  });

  it("saves another model while the previous job is running and attaches the new pending job", async () => {
    const { scope, actions } = faceFixture();
    await actions.attachRecompute("job-1");
    scope.modelKey = "other";
    scope.models.push({ key: "other", bundled_with_baba: true, file_present: true });
    scope.api.saveFaceRecognitionSettings.mockResolvedValue(faceSettings("pending", "other", "job-2"));
    scope.api.getFaceRecomputeStatus.mockResolvedValue({ job_id: "job-2", status: "pending" });
    await actions.save();
    expect(scope.api.saveFaceRecognitionSettings).toHaveBeenCalledWith({
      model_key: "other", detector_key: "yunet", match_threshold: 0.4,
    });
    expect(scope.activeJob).toEqual({ job_id: "job-2", status: "pending" });
    expect(scope.api.startFaceRecompute).not.toHaveBeenCalled();
  });

  it.each(["job-1", "job-2"])("retries a failed selection and resumes job %s", async (jobId) => {
    const { scope, actions } = faceFixture();
    scope.api.saveFaceRecognitionSettings.mockResolvedValue(faceSettings("pending", "new", "job-1"));
    scope.api.getFaceRecomputeStatus.mockResolvedValue({ job_id: "job-1", status: "pending" });
    scope.api.getFaceRecognitionSettings.mockResolvedValue(faceSettings("error", "new", "job-1"));
    await actions.save();
    await actions.pollActivation();
    expect(scope.settings.activation_status).toBe("error");
    scope.api.saveFaceRecognitionSettings.mockResolvedValue(faceSettings("pending", "new", jobId));
    scope.api.getFaceRecomputeStatus.mockResolvedValue({ job_id: jobId, status: "pending" });
    const previousReads = scope.api.getFaceRecomputeStatus.mock.calls.length;
    await actions.save();
    expect(scope.api.saveFaceRecognitionSettings).toHaveBeenCalledTimes(2);
    expect(scope.activeJob).toEqual({ job_id: jobId, status: "pending" });
    expect(scope.api.getFaceRecomputeStatus).toHaveBeenCalledTimes(previousReads + 1);
    expect(scope.api.startFaceRecompute).not.toHaveBeenCalled();
  });

  it("ignores an activation answer that arrives after a newer save", async () => {
    const { scope, actions } = faceFixture();
    await actions.save();
    const late = deferred<ReturnType<typeof faceSettings>>();
    scope.api.getFaceRecognitionSettings.mockImplementationOnce(() => late.promise);
    const polling = actions.pollActivation();
    scope.modelKey = "other";
    scope.models.push({ key: "other", bundled_with_baba: true, file_present: true });
    scope.api.saveFaceRecognitionSettings.mockResolvedValue(faceSettings("pending", "other"));
    await actions.save();
    late.resolve(faceSettings("active"));
    await polling;
    expect(scope.settings.model_key).toBe("other");
    expect(scope.api.startFaceRecompute).not.toHaveBeenCalled();
  });

  it("ignores a manual start answer after a newer model save", async () => {
    const { scope, actions } = faceFixture();
    scope.settings = faceSettings("active");
    const late = deferred<{ job_id: string }>();
    scope.api.startFaceRecompute.mockImplementationOnce(() => late.promise);
    const starting = actions.startRecompute();
    scope.api.saveFaceRecognitionSettings.mockResolvedValue(faceSettings("pending", "new", "job-2"));
    scope.api.getFaceRecomputeStatus.mockResolvedValue({ job_id: "job-2", status: "pending" });
    await actions.save();
    late.resolve({ job_id: "job-1" });
    await starting;
    expect(scope.activeJob).toEqual({ job_id: "job-2", status: "pending" });
    expect(scope.api.getFaceRecomputeStatus).not.toHaveBeenCalledWith("job-1");
  });

  it("does not recreate polling or start a job after disposal during an activation request", async () => {
    const { scope, actions } = faceFixture();
    const late = deferred<ReturnType<typeof faceSettings>>();
    scope.api.getFaceRecognitionSettings.mockImplementation(() => late.promise);
    const polling = actions.pollActivation();
    scope.disposed = true;
    late.resolve(faceSettings("active"));
    await polling;
    expect(scope.api.startFaceRecompute).not.toHaveBeenCalled();
    expect(scope.activationTimer).toBeNull();
  });

  it("keeps a newly started recompute locked until its first status request succeeds", async () => {
    const { scope, actions } = faceFixture();
    scope.settings = faceSettings("active");
    scope.api.getFaceRecognitionSettings.mockResolvedValue(faceSettings("active", "new", "job-1"));
    scope.api.getFaceRecomputeStatus.mockRejectedValueOnce(new Error("offline"));
    await actions.startRecompute();
    expect(scope.startingRecompute).toBe(true);
    await actions.startRecompute();
    expect(scope.api.startFaceRecompute).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1500);
    expect(scope.startingRecompute).toBe(false);
    expect(scope.activeJob).toEqual({ job_id: "job-1", status: "running" });
  });

  it.each(["replaced", "disposed"])("ignores a job response after it is %s", async (kind) => {
    const { scope, actions } = faceFixture();
    const late = deferred<{ job_id: string; status: string }>();
    scope.api.getFaceRecomputeStatus.mockImplementationOnce(() => late.promise);
    const polling = actions.attachRecompute("job-1");
    if (kind === "disposed") scope.disposed = true;
    else await actions.attachRecompute("job-2");
    const expected = scope.activeJob;
    late.resolve({ job_id: "job-1", status: "running" });
    await polling;
    expect(scope.activeJob).toBe(expected);
    if (kind === "disposed") expect(scope.pollTimer).toBeNull();
  });
});
