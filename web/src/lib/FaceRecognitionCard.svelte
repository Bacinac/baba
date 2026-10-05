<script lang="ts">
  import { formatNumber, Button, Card, Notice, Tag, type TagTone, SaveButton } from "$lib/kit";
  import { t } from "$lib/i18n";
  import { onDestroy, onMount } from "svelte";
  import {
    api,
    type FaceModel,
    type FaceDetector,
    type FaceRecognitionSettings,
    type FaceRecomputeStatus,
  } from "$lib/api";
  import { dt } from "$lib/datetime.svelte";

  let models = $state<FaceModel[]>([]);
  let detectors = $state<FaceDetector[]>([]);
  let settings = $state<FaceRecognitionSettings | null>(null);
  let loading = $state(true);
  let saving = $state(false);
  let error = $state<string | null>(null);

  let modelKey = $state("auraface");
  let detectorKey = $state("yunet");
  let threshold = $state(0.4);

  let activeJob = $state<FaceRecomputeStatus | null>(null);
  let startingRecompute = $state(false);
  let pendingRecompute: { model: string; detector: string } | null = null;
  let pollTimer: ReturnType<typeof setTimeout> | null = null;
  let activationTimer: ReturnType<typeof setTimeout> | null = null;
  let disposed = false;
  let settingsSeq = 0;

  function activationReady(): boolean {
    return !!settings && settings.activation_status === "active" &&
      settings.active_model_key === settings.model_key &&
      settings.active_detector_key === settings.detector_key;
  }

  function scheduleActivation() {
    if (activationTimer !== null) clearTimeout(activationTimer);
    activationTimer = null;
    if (!disposed && settings?.activation_status === "pending") {
      activationTimer = setTimeout(pollActivation, 1500);
    }
  }

  async function recomputeIfActivated() {
    if (!pendingRecompute || !settings || disposed) return;
    if (settings.model_key !== pendingRecompute.model ||
        settings.detector_key !== pendingRecompute.detector ||
        settings.activation_status === "error") {
      pendingRecompute = null;
      return;
    }
    if (!activationReady()) return;
    pendingRecompute = null;
    await startRecompute();
  }

  async function pollActivation() {
    if (activationTimer !== null) clearTimeout(activationTimer);
    activationTimer = null;
    const seq = settingsSeq;
    try {
      const current = await api.getFaceRecognitionSettings();
      if (disposed || seq !== settingsSeq) return;
      settings = current;
      await recomputeIfActivated();
    } catch (e) {
      if (!disposed && seq === settingsSeq) error = (e as Error).message;
    } finally {
      if (!disposed && seq === settingsSeq) scheduleActivation();
    }
  }

  async function refresh() {
    const seq = ++settingsSeq;
    loading = true;
    error = null;
    try {
      const [modelsList, detectorsList, current] = await Promise.all([
        api.listFaceModels(),
        api.listFaceDetectors(),
        api.getFaceRecognitionSettings(),
      ]);
      if (disposed || seq !== settingsSeq) return;
      models = modelsList;
      detectors = detectorsList;
      settings = current;
      modelKey = current.model_key;
      detectorKey = current.detector_key;
      threshold = current.match_threshold;
      scheduleActivation();
    } catch (e) {
      if (!disposed && seq === settingsSeq) error = (e as Error).message;
    } finally {
      if (!disposed && seq === settingsSeq) loading = false;
    }
  }

  onMount(refresh);
  onDestroy(() => {
    disposed = true;
    settingsSeq++;
    if (pollTimer !== null) clearTimeout(pollTimer);
    if (activationTimer !== null) clearTimeout(activationTimer);
  });

  function selectedModel(): FaceModel | null {
    return models.find((m) => m.key === modelKey) ?? null;
  }

  function selectedDetector(): FaceDetector | null {
    return detectors.find((d) => d.key === detectorKey) ?? null;
  }

  function hasUnsavedChanges(): boolean {
    if (!settings) return false;
    return (
      modelKey !== settings.model_key ||
      detectorKey !== settings.detector_key ||
      Math.abs(threshold - settings.match_threshold) > 1e-6
    );
  }

  function needsRecompute(): boolean {
    // Only a photo that HAS a face embedding from another model. A face-less
    // reference (pet, vehicle, or a shot with no face found) is not stale —
    // recompute would only stamp the model name on it, and the old check
    // counted those, so the warning could never be cleared and returned with
    // every pet photo added.
    return !!settings && settings.references_stale > 0;
  }

  function modelChangeRequiresRecompute(): boolean {
    return !!settings && (modelKey !== settings.model_key || detectorKey !== settings.detector_key);
  }

  async function save() {
    if (disposed || saving || startingRecompute || activeJob?.status === "running") return;
    const m = selectedModel();
    if (m && !m.bundled_with_baba && !m.file_present) {
      error =
        `${m.name} ${t("fr_weights_missing_prefix")} /models/${m.model_filename}. ` +
        t("fr_weights_missing_suffix");
      return;
    }
    const d = selectedDetector();
    if (d && !d.bundled_with_baba && !d.file_present) {
      error =
        `${d.name} ${t("fr_weights_missing_prefix")} /models/${d.model_filename}. ` +
        t("fr_weights_missing_suffix");
      return;
    }
    const wasModelChange =
      !!settings &&
      (modelKey !== settings.model_key || detectorKey !== settings.detector_key ||
        modelKey !== settings.active_model_key || detectorKey !== settings.active_detector_key);
    const body = { model_key: modelKey, detector_key: detectorKey, match_threshold: threshold };
    const seq = ++settingsSeq;
    if (activationTimer !== null) clearTimeout(activationTimer);
    activationTimer = null;
    saving = true;
    error = null;
    try {
      const current = await api.saveFaceRecognitionSettings(body);
      if (disposed || seq !== settingsSeq) return;
      settings = current;
      modelKey = settings.model_key;
      detectorKey = settings.detector_key;
      threshold = settings.match_threshold;
      if (wasModelChange) {
        pendingRecompute = { model: body.model_key, detector: body.detector_key };
      }
      await recomputeIfActivated();
    } catch (e) {
      if (!disposed && seq === settingsSeq) error = (e as Error).message;
    } finally {
      if (!disposed && seq === settingsSeq) {
        saving = false;
        scheduleActivation();
      }
    }
  }

  async function pollRecompute(jobId: string) {
    pollTimer = null;
    try {
      const current = await api.getFaceRecomputeStatus(jobId);
      if (disposed) return;
      activeJob = current;
      startingRecompute = false;
      if (current.status === "running") {
        pollTimer = setTimeout(() => pollRecompute(jobId), 1500);
      } else {
        await pollActivation();
      }
    } catch (e) {
      if (!disposed) {
        error = (e as Error).message;
        pollTimer = setTimeout(() => pollRecompute(jobId), 1500);
      }
    }
  }

  async function startRecompute() {
    if (disposed || !activationReady() || startingRecompute || activeJob?.status === "running") return;
    startingRecompute = true;
    error = null;
    try {
      const { job_id } = await api.startFaceRecompute();
      if (disposed) return;
      activeJob = null;
      await pollRecompute(job_id);
    } catch (e) {
      if (!disposed) error = (e as Error).message;
    } finally {
      if (!disposed) startingRecompute = pollTimer !== null && activeJob === null;
    }
  }

  function thresholdLabel(v: number): string {
    if (v < 0.30) return t("fr_threshold_very_strict");
    if (v < 0.45) return t("fr_threshold_strict");
    if (v < 0.60) return t("fr_threshold_balanced");
    if (v < 0.80) return t("fr_threshold_loose");
    return t("fr_threshold_very_loose");
  }

  function jobProgressPct(job: FaceRecomputeStatus): number {
    if (job.total === 0) return 0;
    return Math.min(100, Math.round((job.processed / job.total) * 100));
  }

  function jobStatusLabel(status: FaceRecomputeStatus["status"]): string {
    return t(`fr_job_status_${status}`);
  }

  function tierBadge(tier: string): { label: string; tone: TagTone } {
    if (tier === "default") return { label: t("fr_tier_default"), tone: "ok" };
    if (tier === "byom") return { label: "BYOM", tone: "warn" };
    return { label: tier, tone: "quiet" };
  }
</script>

{#if loading}
  <Notice>{t("fr_loading")}</Notice>
{:else if !settings}
  <Notice tone="err">{error ?? t("fr_no_settings")}</Notice>
{:else}
  <div class="space-y-5">
    <!-- Section 1: Model pipeline (detector + embedder, side by side on lg+) -->
    <Card title={t("fr_pipeline")}>
      {#snippet actions()}
        <span class="text-s text-baba-text-faint">{t("fr_pipeline_flow")}</span>
      {/snippet}

      <div class="grid gap-5 lg:grid-cols-2">
        <!-- Detector -->
        <div class="space-y-2">
          <label for="fr-detector" class="block text-m font-medium">{t("fr_detector")}</label>
          <select
            id="fr-detector"
            bind:value={detectorKey}
            disabled={saving || startingRecompute || activeJob?.status === "running"}
            class="block w-full rounded border border-baba-border bg-baba-bg px-3 py-2 text-m focus:outline-none focus:ring-1 focus:ring-baba-accent"
          >
            {#each detectors as d (d.key)}
              <option value={d.key} disabled={!d.bundled_with_baba && !d.file_present}>
                {d.name}{!d.bundled_with_baba && !d.file_present ? t("fr_file_missing_suffix") : ""}
              </option>
            {/each}
          </select>
          {#if selectedDetector()}
            {@const det = selectedDetector()!}
            {@const tb = tierBadge(det.tier)}
            <div class="flex flex-wrap items-center gap-2 text-s">
              <Tag tone={tb.tone}>{tb.label}</Tag>
              <span class="text-baba-text-faint">{det.license}</span>
              {#if !det.bundled_with_baba && !det.file_present}
                <Tag tone="err">{t("fr_file_missing_badge")}</Tag>
              {/if}
            </div>
            <p class="text-s leading-relaxed text-baba-text-muted">
              {det.description}
            </p>
          {/if}
        </div>

        <!-- Embedder -->
        <div class="space-y-2">
          <label for="fr-embedder" class="block text-m font-medium">{t("fr_embedder")}</label>
          <select
            id="fr-embedder"
            bind:value={modelKey}
            disabled={saving || startingRecompute || activeJob?.status === "running"}
            class="block w-full rounded border border-baba-border bg-baba-bg px-3 py-2 text-m focus:outline-none focus:ring-1 focus:ring-baba-accent"
          >
            {#each models as m (m.key)}
              <option value={m.key} disabled={!m.bundled_with_baba && !m.file_present}>
                {m.name}{!m.bundled_with_baba && !m.file_present ? t("fr_file_missing_suffix") : ""}
              </option>
            {/each}
          </select>
          {#if selectedModel()}
            {@const m = selectedModel()!}
            {@const tb = tierBadge(m.tier)}
            <div class="flex flex-wrap items-center gap-2 text-s">
              <Tag tone={tb.tone}>{tb.label}</Tag>
              <span class="text-baba-text-faint">{m.license}</span>
              {#if !m.bundled_with_baba && !m.file_present}
                <Tag tone="err">{t("fr_file_missing_badge")}</Tag>
              {/if}
            </div>
            <p class="text-s leading-relaxed text-baba-text-muted">
              {m.description}
            </p>
          {/if}
        </div>
      </div>
    </Card>

    <!-- Section 2: Threshold -->
    <Card title={t("fr_match_threshold")}>
      {#snippet actions()}
        <span class="font-mono text-m">{formatNumber(threshold, { minimumFractionDigits: 2, maximumFractionDigits: 2 })} <span class="text-baba-text-faint">· {thresholdLabel(threshold)}</span></span>
      {/snippet}
      <input
        type="range"
        min="0.10"
        max="1.20"
        step="0.01"
        bind:value={threshold}
        disabled={saving || startingRecompute || activeJob?.status === "running"}
        class="w-full"
      />
      <p class="mt-2 text-s text-baba-text-faint">
        {t("fr_threshold_help")}
      </p>
    </Card>

    <!-- Section 3: Status -->
    <Card title={t("fr_status")}>
      {#snippet actions()}
        {#if settings}<span class="text-s text-baba-text-faint">{t("fr_updated")} {dt.full(settings.updated_at)}</span>{/if}
      {/snippet}
      {#if settings.activation_status === "pending"}
        <Notice>{t("fr_activation_pending")}</Notice>
      {:else if settings.activation_status === "error"}
        <Notice tone="err">
          <p>{t("fr_activation_error")}</p>
          {#if settings.activation_error}<p>{settings.activation_error}</p>{/if}
        </Notice>
      {/if}
      <dl class="grid grid-cols-2 gap-x-6 gap-y-2 text-m md:grid-cols-3">
        <div>
          <dt class="text-s text-baba-text-faint">{t("fr_selected_detector")}</dt>
          <dd class="font-mono">{settings.detector_key}</dd>
        </div>
        <div>
          <dt class="text-s text-baba-text-faint">{t("fr_selected_embedder")}</dt>
          <dd class="font-mono">{settings.model_key}</dd>
        </div>
        <div>
          <dt class="text-s text-baba-text-faint">{t("fr_active_detector")}</dt>
          <dd class="font-mono">{settings.active_detector_key}</dd>
        </div>
        <div>
          <dt class="text-s text-baba-text-faint">{t("fr_active_embedder")}</dt>
          <dd class="font-mono">{settings.active_model_key}</dd>
        </div>
        <div>
          <dt class="text-s text-baba-text-faint">{t("fr_reference_coverage")}</dt>
          <dd class="font-mono">
            {settings.references_in_active_model} / {settings.references_total -
              settings.references_no_face}
          </dd>
          {#if settings.references_no_face > 0}
            <!-- Stated plainly, not as a warning: these are working references,
                 matched by body embedding. -->
            <div class="mt-0.5 text-xs text-baba-text-faint">
              {t("fr_refs_no_face_prefix")}
              {settings.references_no_face}
              {t("fr_refs_no_face_suffix")}
            </div>
          {/if}
        </div>
      </dl>

      {#if needsRecompute() || modelChangeRequiresRecompute()}
        <div class="mt-3 rounded border border-amber-700/50 bg-amber-950/30 px-3 py-2 text-s text-amber-300">
          {#if modelChangeRequiresRecompute()}
            {t("fr_recompute_on_save")}
          {:else}
            {t("fr_recompute_needed_prefix")}
            <strong>{settings.references_stale}</strong>.
            {t("fr_recompute_needed_suffix")}
          {/if}
        </div>
      {/if}
    </Card>

    <!-- Section 4: Actions + active job -->
    <Card>
      <div class="flex flex-wrap items-center justify-between gap-3">
        <p class="text-s text-baba-text-faint">
          {t("fr_byom_note")} <code class="rounded bg-baba-bg px-1">/models/</code>.
        </p>
        <div class="flex flex-wrap gap-2">
          <Button onclick={startRecompute} disabled={!activationReady() || saving || startingRecompute || activeJob?.status === "running"} title={t("fr_action_force_recompute_title")}>
            {t("fr_action_force_recompute")}
          </Button>
          <SaveButton dirty={hasUnsavedChanges() || settings.activation_status === "error"} {saving} blocked={startingRecompute || activeJob?.status === "running"} label={settings.activation_status === "error" && !hasUnsavedChanges() ? t("fr_retry_activation") : undefined} onclick={save} />
        </div>
      </div>

      {#if activeJob}
        <div class="mt-4 rounded border border-baba-border bg-baba-bg/50 px-3 py-3 text-s">
          <div class="flex items-center justify-between gap-3">
            <span class="text-baba-text-muted">
              <span class="font-mono">{activeJob.model_key}</span> · {jobStatusLabel(activeJob.status)}
            </span>
            <span class="font-mono">
              {activeJob.processed} / {activeJob.total} ({jobProgressPct(activeJob)}%)
            </span>
          </div>
          <div class="mt-2 h-1.5 w-full overflow-hidden rounded bg-baba-border/50">
            <div
              class="h-full bg-baba-accent transition-all"
              style="width: {jobProgressPct(activeJob)}%"
            ></div>
          </div>
          {#if activeJob.status === "done"}
            <p class="mt-2 text-emerald-400">
              ✓ {activeJob.succeeded} {t("fr_job_embedded")} · {activeJob.no_face} {t("fr_job_no_face")} ·
              {activeJob.missing_file} {t("fr_job_missing")}
            </p>
          {:else if activeJob.status === "failed"}
            <p class="mt-2 text-red-400">
              ✗ {activeJob.error_message ?? t("fr_job_unknown_error")}
            </p>
          {/if}
        </div>
      {/if}

      {#if error}
        <p class="mt-3 text-m text-red-400">{error}</p>
      {/if}
    </Card>
  </div>
{/if}
