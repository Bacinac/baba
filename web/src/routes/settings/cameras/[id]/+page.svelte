<script lang="ts">
  import { goto } from "$app/navigation";
  import { dialog, Button, Tabs, Card, Tag, SaveButton } from "$lib/kit";
  import { page } from "$app/state";
  import { onMount } from "svelte";
  import { api, type Camera, type CameraPatch, type TrackingDefaults } from "$lib/api";
  import { t, type MessageKey } from "$lib/i18n";
  import StreamUrlField from "$lib/StreamUrlField.svelte";
  import LiveStream from "$lib/LiveStream.svelte";
  import CameraDetectionRulesCard from "$lib/CameraDetectionRulesCard.svelte";
  import ZoneEditor from "$lib/ZoneEditor.svelte";
  import SceneStates from "$lib/SceneStates.svelte";

  let id = $derived(page.params.id as string);

  // Tabs: "camera" = connection/plumbing (RTSP, fps, recording);
  // "zones" = what/where we watch (detection rules + zone editor).
  // Deep-linkable via ?tab=zones so the retired /settings/zones-rules route
  // and in-app links can land straight on the editor.
  type CameraTab = "camera" | "zones" | "states";
  function tabFromUrl(): CameraTab {
    const t = page.url.searchParams.get("tab");
    return t === "zones" || t === "states" ? t : "camera";
  }
  let activeTab = $state<CameraTab>(tabFromUrl());

  function setTab(t: CameraTab) {
    activeTab = t;
    // Keep the URL in sync so the tab survives reload / bookmark / back.
    const url = new URL(page.url);
    if (t === "camera") url.searchParams.delete("tab");
    else url.searchParams.set("tab", t);
    goto(url, { replaceState: true, keepFocus: true, noScroll: true });
  }

  let cam = $state<Camera | null>(null);
  let loading = $state(true);
  let loadError = $state<string | null>(null);

  // Editable fields — initialised from `cam` on load. We keep them as a
  // separate state so the form can be dirtied + submitted without mutating
  // the source-of-truth `cam` directly.
  let edit = $state<CameraPatch>({});
  // A cleared field binds as "" where the camera holds null; both mean empty.
  const blank = (v: unknown) => (v === "" || v === undefined ? null : v);
  const dirty = $derived.by(() => {
    const c = cam;
    return c !== null && Object.entries(edit).some(([k, v]) => blank(v) !== blank(c[k as keyof Camera]));
  });
  let saving = $state(false);
  let saveError = $state<string | null>(null);
  let savedAt = $state<number | null>(null);

  // Slug rename — gated behind a toggle because it touches the
  // filesystem and we want the operator to opt in deliberately. Slug
  // lives outside the regular PATCH cycle since it goes through a
  // dedicated endpoint that coordinates the disk rename.
  let renaming = $state(false);
  let renameDraft = $state("");
  let renameSaving = $state(false);
  let renameError = $state<string | null>(null);

  async function load() {
    const myseq = ++loadSeq;
    loading = true;
    loadError = null;
    try {
      const c = await api.getCamera(id);
      if (myseq !== loadSeq) return; // superseded by a newer id
      cam = c;
      edit = {
        name: c.name,
        stream_url: c.stream_url,
        substream_url: c.substream_url,
        analysis_stream: c.analysis_stream,
        enabled: c.enabled,
        target_fps: c.target_fps,
        idle_fps: c.idle_fps,
        downscale_max_edge: c.downscale_max_edge,
        recording_enabled: c.recording_enabled,
        color: c.color,
      };
    } catch (e) {
      if (myseq === loadSeq) loadError = (e as Error).message;
    } finally {
      if (myseq === loadSeq) loading = false;
    }
  }

  async function renameSlug(e?: Event) {
    // The rename submit lives inside the outer camera-edit form, so we
    // route it through a regular <div> + button to dodge the nested-
    // <form> SSR warning.  An optional event allows callers from both
    // an Enter-keypress in the input and a button click.
    e?.preventDefault();
    if (!cam) return;
    const next = renameDraft.trim();
    if (!next || next === cam.slug) {
      renaming = false;
      return;
    }
    renameSaving = true;
    renameError = null;
    try {
      cam = await api.renameCameraSlug(cam.id, next);
      renaming = false;
      renameDraft = "";
    } catch (err) {
      renameError = (err as Error).message;
    } finally {
      renameSaving = false;
    }
  }

  async function save(e: SubmitEvent) {
    e.preventDefault();
    if (!cam) return;
    saving = true;
    saveError = null;
    try {
      cam = await api.patchCamera(cam.id, edit);
      savedAt = Date.now();
      // Clear "saved" flag after a moment.
      setTimeout(() => {
        if (savedAt !== null && Date.now() - savedAt >= 1900) savedAt = null;
      }, 2000);
    } catch (err) {
      saveError = (err as Error).message;
    } finally {
      saving = false;
    }
  }

  async function remove() {
    if (!cam) return;
    const ok = await dialog.confirm({
      title: t("camera_delete"),
      message: `${t("cameras_confirm_delete")} "${cam.name}"?`,
      confirmLabel: t("dialog_delete"),
      danger: true,
    });
    if (!ok) return;
    try {
      await api.deleteCamera(cam.id);
      goto("/settings/cameras", { replaceState: true });
    } catch (err) {
      await dialog.alert({
        title: t("dialog_error_title"),
        message: `${t("cameras_error_prefix")}: ${(err as Error).message}`,
      });
    }
  }

  // Tracking sliders live on the zones tab next to the detection rules and
  // save IMMEDIATELY on release (same interaction as the rules sliders) — the
  // operator tunes them while watching the live preview, so a form-submit
  // round trip would be friction in the exact moment of calibration.
  //
  // All four knobs are LAYERED: a null camera value inherits the global
  // tracking defaults (Settings → Detection); moving a slider writes a
  // per-camera override; ↺ clears it back to inherit.
  const LIGHT_KEYS: Record<string, MessageKey> = {
    ir: "light_ir",
    dark: "light_dark",
    dim: "light_dim",
    normal: "light_normal",
    bright: "light_bright",
  };
  function lightLabel(cond: string): string {
    const key = LIGHT_KEYS[cond];
    return key ? t(key) : cond;
  }
  type TrackingField = "stillness_ratio" | "park_seconds" | "lost_seconds" | "reid_lost_seconds";
  const TRACKING_ROWS: {
    key: TrackingField;
    label: MessageKey;
    hint: MessageKey;
    min: number; max: number; step: number;
    fmt: (v: number) => string;
    parse: (s: string) => number;
  }[] = [
    {
      key: "stillness_ratio", label: "camera_stillness_ratio", hint: "camera_stillness_ratio_hint",
      min: 0.05, max: 0.3, step: 0.01,
      fmt: (v) => `${Math.round(v * 100)}%`, parse: (s) => parseFloat(s),
    },
    {
      key: "park_seconds", label: "camera_park_seconds", hint: "camera_park_seconds_hint",
      min: 15, max: 300, step: 5,
      fmt: (v) => `${v}s`, parse: (s) => parseInt(s, 10),
    },
    {
      key: "lost_seconds", label: "camera_lost_seconds", hint: "camera_lost_seconds_hint",
      min: 5, max: 120, step: 5,
      fmt: (v) => `${v}s`, parse: (s) => parseInt(s, 10),
    },
    {
      key: "reid_lost_seconds", label: "camera_reid_lost_seconds", hint: "camera_reid_lost_seconds_hint",
      min: 0, max: 300, step: 10,
      fmt: (v) => `${v}s`, parse: (s) => parseInt(s, 10),
    },
  ];
  let trackingGlobals = $state<TrackingDefaults | null>(null);
  let trackingDrag = $state<Partial<Record<TrackingField, number>>>({});
  let stillnessSaving = $state(false);
  // Maintain floor is per-camera and NOT layered (no inherit-global) — always
  // a concrete value, stamped into the current light-band profile on release.
  let maintainDrag = $state<number | null>(null);
  async function saveMaintain(v: number) {
    if (!cam) return;
    stillnessSaving = true;
    try {
      cam = await api.patchCamera(cam.id, { maintain_conf: v });
    } catch (e) {
      saveError = (e as Error).message;
    } finally {
      stillnessSaving = false;
      maintainDrag = null;
    }
  }
  onMount(async () => {
    try {
      trackingGlobals = await api.getTrackingDefaults();
    } catch {
      trackingGlobals = null; // badge/effective falls back to camera values
    }
  });
  function trackingEffective(field: TrackingField): number {
    return (
      trackingDrag[field] ??
      cam?.[field] ??
      trackingGlobals?.[field] ??
      { stillness_ratio: 0.08, park_seconds: 60, lost_seconds: 30, reid_lost_seconds: 120 }[field]
    );
  }
  async function saveTracking(patch: Partial<Record<TrackingField, number | null>>) {
    if (!cam) return;
    stillnessSaving = true;
    try {
      cam = await api.patchCamera(cam.id, patch);
    } catch (e) {
      saveError = (e as Error).message;
    } finally {
      stillnessSaving = false;
      trackingDrag = {};
    }
  }

  // Load on mount and whenever the URL id changes (rare, via in-app nav). The
  // $effect already runs once on mount, so a separate onMount(load) would just
  // double-fetch. Monotonic guard so a rapid id change settles newest-wins.
  let loadSeq = 0;
  $effect(() => { void id; void load(); });
</script>

<div class="mb-4">
  <a href="/settings/cameras" class="text-s text-baba-text-faint hover:text-baba-text-muted">
    {t("camera_back_to_list")}
  </a>
</div>

{#if loading}
  <p class="text-baba-text-faint">{t("cameras_loading")}</p>
{:else if loadError || !cam}
  <p class="text-red-400">{loadError ?? "camera not found"}</p>
{:else}
  <div class="mb-6 flex items-start justify-between gap-4">
    <div>
      <h2 class="flex items-center gap-2 text-2xl font-semibold">
        <span
          class="inline-block h-3 w-3 rounded-full ring-1 ring-baba-border"
          style="background-color: {edit.color ?? cam.color}"
          aria-hidden="true"
        ></span>
        {edit.name ?? cam.name}
      </h2>
      <p class="mt-1 text-s text-baba-text-faint font-mono">{cam.slug}</p>
    </div>
    <!-- Header preview only on the Kamera tab. The "Zone i detekcija" tab
         renders its own full-width LiveStream inside the zone editor; showing
         this one too would decode the same RTSP stream twice and stutter. -->
    {#if activeTab === "camera"}
      <div class="w-72 shrink-0">
        <div class="aspect-video overflow-hidden rounded bg-black">
          <LiveStream
            slug={cam.slug}
            preferSubstream
            hasSubstream={!!cam.substream_url}
            class="h-full w-full object-cover"
          />
        </div>
      </div>
    {/if}
  </div>

  <!-- Tabs: connection/plumbing vs. what+where we watch -->
  <div class="mb-6">
    <Tabs
      tabs={[
        { key: "camera", label: t("camera_tab_camera") },
        { key: "zones", label: t("camera_tab_zones") },
        { key: "states", label: t("scene_states_tab") },
      ]}
      active={activeTab}
      onpick={(k) => setTab(k as typeof activeTab)}
    />
  </div>

  {#if activeTab === "camera"}
  <form class="space-y-8 max-w-3xl" onsubmit={save}>
    <!-- Basics -->
    <section class="space-y-3">
      <h3 class="text-m font-semibold uppercase tracking-wide text-baba-text-muted">
        {t("camera_section_basic")}
      </h3>
      <div class="grid grid-cols-1 gap-3 md:grid-cols-2">
        <label class="block">
          <span class="block text-s text-baba-text-muted">{t("add_camera_name")}</span>
          <input
            bind:value={edit.name}
            required
            class="w-full rounded border border-baba-border bg-baba-panel-2 px-2 py-1.5 text-m"
          />
        </label>
        <label class="flex items-center gap-2 self-end pb-1.5 text-m text-baba-text-muted">
          <input type="checkbox" bind:checked={edit.enabled} class="accent-baba-accent" />
          {t("camera_enabled")}
        </label>
        <label class="block">
          <span class="block text-s text-baba-text-muted">{t("camera_color")}</span>
          <div class="mt-1 flex items-center gap-2">
            <input
              type="color"
              value={edit.color ?? "#f59e0b"}
              oninput={(e) => (edit.color = (e.currentTarget as HTMLInputElement).value)}
              class="h-8 w-12 cursor-pointer rounded border border-baba-border bg-baba-panel-2 p-0.5"
            />
            <span class="font-mono text-s uppercase text-baba-text-faint">{edit.color ?? cam.color}</span>
          </div>
          <p class="mt-1 text-xs text-baba-text-faint">{t("camera_color_hint")}</p>
        </label>
      </div>
    </section>

    <!-- Identifier -->
    <section class="space-y-3">
      <h3 class="text-m font-semibold uppercase tracking-wide text-baba-text-muted">
        {t("camera_section_identity")}
      </h3>
      <div>
        <span class="block text-s text-baba-text-muted">{t("camera_slug")}</span>
        {#if !renaming}
          <div class="mt-1 flex items-center gap-2">
            <code class="rounded border border-baba-border bg-baba-panel-2 px-2 py-1 text-m">{cam.slug}</code>
            <Button size="small" onclick={() => {
                if (!cam) return;
                renameDraft = cam.slug;
                renameError = null;
                renaming = true;
              }}>{t("camera_slug_rename")}</Button>
          </div>
          <p class="mt-1 text-xs text-baba-text-faint">{t("camera_slug_hint")}</p>
        {:else}
          <div
            class="mt-1 rounded border border-amber-500/30 bg-amber-500/5 p-3"
            role="group"
            aria-labelledby="rename-title"
          >
            <p id="rename-title" class="text-m font-medium text-amber-300">
              {t("camera_slug_rename_title")}
            </p>
            <p class="mt-1 text-s text-baba-text-muted">
              {t("camera_slug_rename_warning")}
            </p>
            <div class="mt-3 flex items-center gap-2">
              <input
                type="text"
                bind:value={renameDraft}
                required
                minlength="1"
                maxlength="64"
                pattern="^[a-z0-9][a-z0-9_-]*$"
                autocomplete="off"
                spellcheck="false"
                disabled={renameSaving}
                onkeydown={(e) => {
                  if (e.key === "Enter") { e.preventDefault(); renameSlug(); }
                }}
                class="flex-1 rounded border border-baba-border bg-baba-panel-2 px-2 py-1 text-m font-mono"
                placeholder={cam.slug}
              />
              <SaveButton size="small" dirty={!!renameDraft.trim() && renameDraft.trim() !== cam.slug} saving={renameSaving} label={t("camera_slug_rename_confirm")} onclick={() => renameSlug()} />
              <Button size="small" onclick={() => { renaming = false; renameError = null; }} disabled={renameSaving}>{t("discover_cancel")}</Button>
            </div>
            <p class="mt-2 text-xs text-baba-text-faint">{t("camera_slug_pattern_hint")}</p>
            {#if renameError}
              <p class="mt-2 text-s text-red-400">{renameError}</p>
            {/if}
          </div>
        {/if}
      </div>
    </section>

    <!-- Stream source -->
    <section class="space-y-3">
      <h3 class="text-m font-semibold uppercase tracking-wide text-baba-text-muted">
        {t("camera_section_stream")}
      </h3>
      <StreamUrlField
        streamUrl={edit.stream_url as string}
        substreamUrl={edit.substream_url ?? null}
        analysisStream={edit.analysis_stream ?? "main"}
        onChange={(v) => Object.assign(edit, v)}
        disabled={saving}
      />
    </section>

    <!-- Pipeline -->
    <section class="space-y-3">
      <h3 class="text-m font-semibold uppercase tracking-wide text-baba-text-muted">
        {t("camera_section_pipeline")}
      </h3>
      <div class="grid grid-cols-2 gap-3 md:grid-cols-3">
        <label class="block">
          <span class="block text-s text-baba-text-muted">{t("add_camera_target_fps")}</span>
          <input
            type="number" min="1" max="30"
            bind:value={edit.target_fps}
            class="w-full rounded border border-baba-border bg-baba-panel-2 px-2 py-1.5 text-m"
          />
        </label>
        <label class="block">
          <span class="block text-s text-baba-text-muted">{t("camera_idle_fps")}</span>
          <input
            type="number" min="1" max={(edit.target_fps ?? 30) - 1}
            value={edit.idle_fps ?? ""}
            oninput={(e) => {
              const v = (e.currentTarget as HTMLInputElement).value;
              edit.idle_fps = v === "" ? null : Number(v);
            }}
            class="w-full rounded border border-baba-border bg-baba-panel-2 px-2 py-1.5 text-m"
            placeholder="—"
          />
          <p class="mt-1 text-xs text-baba-text-faint">{t("camera_idle_fps_hint")}</p>
        </label>
        <label class="block">
          <span class="block text-s text-baba-text-muted">{t("add_camera_max_edge")}</span>
          <input
            type="number" min="0"
            bind:value={edit.downscale_max_edge}
            class="w-full rounded border border-baba-border bg-baba-panel-2 px-2 py-1.5 text-m"
          />
          <p class="mt-1 text-xs text-baba-text-faint">{t("camera_max_edge_hint")}</p>
        </label>
      </div>
    </section>

    <!-- Recording -->
    <section class="space-y-3">
      <h3 class="text-m font-semibold uppercase tracking-wide text-baba-text-muted">
        {t("camera_section_recording")}
      </h3>
      <div class="flex flex-wrap items-end gap-6">
        <label class="flex items-center gap-2 pb-1.5 text-m text-baba-text-muted">
          <input type="checkbox" bind:checked={edit.recording_enabled} class="accent-baba-accent" />
          {t("camera_recording_enabled")}
        </label>
      </div>
    </section>

    {#if saveError}
      <p class="text-m text-red-400">{saveError}</p>
    {/if}

    <div class="flex items-center gap-3">
      <SaveButton type="submit" {dirty} {saving} />
      {#if savedAt}
        <span class="text-s text-emerald-400">✓ {t("camera_saved")}</span>
      {/if}
    </div>
  </form>

  <!-- Danger zone -->
  <section class="mt-12 max-w-3xl rounded-lg border border-red-500/30 bg-red-500/5 p-4">
    <h3 class="text-m font-semibold uppercase tracking-wide text-red-400">
      {t("camera_section_danger")}
    </h3>
    <p class="mt-1 text-s text-baba-text-faint">
      {t("camera_danger_cascade")}
    </p>
    <div class="mt-3"><Button tone="danger" onclick={remove}>{t("camera_delete")}</Button></div>
  </section>
  {/if}

  {#if activeTab === "zones"}
    <!-- What: per-camera detection rules (layer 2) — which classes + confidence.
         Empty per-camera list = inherits the global Settings → AI rules. -->
    <section class="max-w-3xl">
      <CameraDetectionRulesCard cameraId={id} />
    </section>

    <!-- Stillness tuning — lives HERE with the other per-camera knobs so the
         operator adjusts it while watching the preview; saves on release. -->
    {#if cam}
      <section class="mt-6 max-w-3xl">
        <Card title={t("camera_section_stillness")}>
          {#snippet actions()}
            <!-- Measured illumination band — tuning done now is stamped into
                 this band's profile and re-applied when light returns to it. -->
            {#if cam}
              <span class="text-s text-baba-text-muted">
                {t("light_condition_label")}:
                <Tag tone="quiet">{lightLabel(cam.light_condition)}</Tag>
              </span>
            {/if}
          {/snippet}
          <div class="mt-3 grid grid-cols-1 gap-4 md:grid-cols-2">
            {#each TRACKING_ROWS as r (r.key)}
              <label class="block">
                <span class="flex items-center gap-2 text-s text-baba-text-muted">
                  {t(r.label)}
                  {#if cam[r.key] !== null}
                    <Tag tone="busy">
                      {t("tracking_override_camera")}
                    </Tag>
                    <Button size="small" onclick={() => saveTracking({ [r.key]: null })} disabled={stillnessSaving}
                      title={t("tracking_revert_to_global")} label={t("tracking_revert_to_global")}>↺</Button>
                  {:else}
                    <Tag tone="quiet">
                      {t("tracking_inherit_global")}
                    </Tag>
                  {/if}
                </span>
                <div class="mt-1 flex items-center gap-3">
                  <input
                    type="range" min={r.min} max={r.max} step={r.step}
                    value={trackingEffective(r.key)}
                    disabled={stillnessSaving}
                    oninput={(e) => (trackingDrag = { ...trackingDrag, [r.key]: r.parse((e.target as HTMLInputElement).value) })}
                    onchange={(e) => saveTracking({ [r.key]: r.parse((e.target as HTMLInputElement).value) })}
                    class="w-full accent-sky-500"
                  />
                  <span class="w-12 text-right font-mono text-s text-baba-text-faint">
                    {r.fmt(trackingEffective(r.key))}
                  </span>
                </div>
                <p class="mt-1 text-xs text-baba-text-faint">{t(r.hint)}</p>
              </label>
            {/each}
          </div>
          <!-- Maintain floor — per-camera, not layered. Lower it under night IR
               (this band's profile stamps it) so a still person's decayed
               detections still keep the track alive; birth is untouched. -->
          <div class="mt-4 border-t border-baba-border pt-4">
            <label class="block">
              <span class="text-s text-baba-text-muted">{t("camera_maintain_conf")}</span>
              <div class="mt-1 flex items-center gap-3">
                <input
                  type="range" min={0.05} max={0.55} step={0.01}
                  value={maintainDrag ?? cam.maintain_conf}
                  disabled={stillnessSaving}
                  oninput={(e) => (maintainDrag = parseFloat((e.target as HTMLInputElement).value))}
                  onchange={(e) => saveMaintain(parseFloat((e.target as HTMLInputElement).value))}
                  class="w-full accent-sky-500"
                />
                <span class="w-12 text-right font-mono text-s text-baba-text-faint">
                  {Math.round((maintainDrag ?? cam.maintain_conf) * 100)}%
                </span>
              </div>
              <p class="mt-1 text-xs text-baba-text-faint">{t("camera_maintain_conf_hint")}</p>
            </label>
          </div>
        </Card>
      </section>
    {/if}
    <!-- Where + dwell + zone-specific rules (layer 3). No zones = whole frame. -->
    <section class="mt-12">
      <ZoneEditor cameraId={id} />
    </section>
  {/if}

  {#if activeTab === "states" && cam}
    <!-- Persistent state of fixed scene structure (gate open/closed, …),
         few-shot via DINOv2 prototypes — see services/state-evaluator. -->
    <section class="max-w-3xl">
      <SceneStates cameraId={id} cameraSlug={cam.slug} hasSubstream={!!cam.substream_url} />
    </section>
  {/if}
{/if}
