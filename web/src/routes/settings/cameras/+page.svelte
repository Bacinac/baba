<script lang="ts">
  import { formatNumber, Button, Card, Toggle } from "$lib/kit";
  import { t } from "$lib/i18n";
  import { onMount, onDestroy } from "svelte";
  import { goto } from "$app/navigation";
  import { api, type Camera, type CameraIn, type LiveStatus } from "$lib/api";
  import StreamUrlField from "$lib/StreamUrlField.svelte";
  import DiscoverPanel from "$lib/DiscoverPanel.svelte";
  import LiveStream from "$lib/LiveStream.svelte";
  import { liveOverlay } from "$lib/liveOverlay.svelte";

  let overlaySaving = $state(false);
  async function saveOverlay(patch: { boxes?: boolean; badges?: boolean }) {
    overlaySaving = true;
    try {
      await liveOverlay.save({
        boxes: patch.boxes ?? liveOverlay.boxes,
        badges: patch.badges ?? liveOverlay.badges,
      });
    } finally {
      overlaySaving = false;
    }
  }

  let cameras = $state<Camera[]>([]);
  let loading = $state(true);
  let error = $state<string | null>(null);

  // slug → live status, so the card shows the MEASURED effective fps (adaptive,
  // what the camera actually runs at right now) rather than the static
  // target_fps config — same source as the Live grid.
  let status = $state<Record<string, LiveStatus>>({});
  let pollTimer: ReturnType<typeof setInterval> | null = null;
  async function pollStatus() {
    try {
      status = await api.getLiveStatus();
    } catch {
      // transient — keep the last values rather than blanking the fps labels
    }
  }

  // Map host (from stream_url) → camera, so DiscoverPanel can mark already-added rows.
  // Matches the common case (IP in stream_url); hostname-vs-IP and multi-stream NVRs
  // won't match and will fall through as "addable" — acceptable for now.
  let existingByHost = $derived.by(() => {
    const map = new Map<string, Camera>();
    for (const cam of cameras) {
      try {
        const host = new URL(cam.stream_url).hostname;
        if (host) map.set(host, cam);
      } catch {
        // malformed stream_url — skip
      }
    }
    return map;
  });

  let form = $state<CameraIn>({
    slug: "",
    name: "",
    stream_url: "",
    substream_url: null,
    analysis_stream: "main",
    target_fps: 5,
    downscale_max_edge: 1280,
    enabled: true,
    recording_enabled: true,
  });
  let formError = $state<string | null>(null);
  let formBusy = $state(false);
  let showManualForm = $state(false);

  async function refresh() {
    loading = true;
    error = null;
    try {
      cameras = await api.listCameras();
    } catch (e) {
      error = (e as Error).message;
    } finally {
      loading = false;
    }
  }

  async function addCamera(e: SubmitEvent) {
    e.preventDefault();
    formBusy = true;
    formError = null;
    try {
      const cam = await api.createCamera(form);
      form = {
        slug: "", name: "", stream_url: "", substream_url: null, analysis_stream: "main",
        target_fps: 5, downscale_max_edge: 1280,
        enabled: true, recording_enabled: true,
      };
      await refresh();
      // Jump straight to detail page of the new camera so user can fine-tune.
      goto(`/settings/cameras/${cam.id}`);
    } catch (err) {
      formError = (err as Error).message;
    } finally {
      formBusy = false;
    }
  }

  onMount(() => {
    refresh();
    liveOverlay.load();
    pollStatus();
    pollTimer = setInterval(pollStatus, 1000);
  });
  onDestroy(() => {
    if (pollTimer !== null) clearInterval(pollTimer);
  });
</script>

<!-- Global live-view overlay toggles: apply to the grid tiles AND the
     per-camera full-stream view. One setting for the whole system. -->
<div class="mb-6"><Card title={t("live_overlay_title")}>
  <p class="mt-1 text-s text-baba-text-faint">{t("live_overlay_desc")}</p>
  <div class="mt-3 flex flex-wrap gap-6">
    <label class="inline-flex items-center gap-2 text-m">
      <Toggle size="small" checked={liveOverlay.boxes} disabled={overlaySaving} onclick={() => saveOverlay({ boxes: !liveOverlay.boxes })} />
      {t("live_overlay_boxes")}
    </label>
    <label class="inline-flex items-center gap-2 text-m">
      <Toggle size="small" checked={liveOverlay.badges} disabled={overlaySaving} onclick={() => saveOverlay({ badges: !liveOverlay.badges })} />
      {t("live_overlay_badges")}
    </label>
  </div>
</Card></div>

{#if loading}
  <p class="text-baba-text-faint">{t("cameras_loading")}</p>
{:else if error}
  <p class="text-red-400">{error}</p>
{:else if cameras.length === 0}
  <p class="text-baba-text-faint">{t("cameras_empty")}</p>
{:else}
  <ul class="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
    {#each cameras as cam (cam.id)}
      <li class="rounded-lg border border-baba-border bg-baba-panel p-3">
        <a class="block" href={`/settings/cameras/${cam.id}`}>
          <div class="mb-3 aspect-video overflow-hidden rounded bg-black">
            <LiveStream
              slug={cam.slug}
              preferSubstream
              hasSubstream={!!cam.substream_url}
              class="h-full w-full object-cover"
            />
          </div>
        </a>
        <div class="flex items-center justify-between">
          <div>
            <div class="flex items-center gap-2 font-medium">
              <span
                class="inline-block h-2.5 w-2.5 rounded-sm ring-1 ring-baba-border"
                style="background-color: {cam.color}"
                aria-hidden="true"
              ></span>
              {cam.name}
            </div>
            <div class="text-s text-baba-text-faint">
              {formatNumber(status[cam.slug]?.fps ?? cam.target_fps, { minimumFractionDigits: 1, maximumFractionDigits: 1 })} {t("cameras_fps_suffix")} ·
              {cam.enabled ? t("cameras_status_enabled") : t("cameras_status_disabled")}
            </div>
          </div>
          <a
            href={`/settings/cameras/${cam.id}`}
            class="rounded border border-baba-border bg-baba-panel-2 px-2 py-1 text-s text-baba-text-muted hover:bg-baba-panel"
          >{t("camera_open_settings")}</a>
        </div>
      </li>
    {/each}
  </ul>
{/if}

<section class="mt-10 max-w-3xl">
  <DiscoverPanel onCameraAdded={refresh} {existingByHost} />
</section>

<section class="mt-6 max-w-3xl">
  <button
    type="button"
    onclick={() => (showManualForm = !showManualForm)}
    class="flex items-center gap-2 text-m text-baba-text-muted hover:text-baba-text"
  >
    <span class="transition-transform duration-150" class:rotate-90={showManualForm}>▶</span>
    {t("add_manually_toggle")}
  </button>
</section>

{#if showManualForm}
<section id="add-camera-form" class="mt-4 max-w-3xl">
  <form class="space-y-4" onsubmit={addCamera}>
    <div class="grid grid-cols-1 gap-3 md:grid-cols-2">
      <label class="block">
        <span class="block text-s text-baba-text-muted">{t("add_camera_slug")}</span>
        <input
          bind:value={form.slug}
          required
          pattern="[a-z0-9][a-z0-9_-]*"
          class="w-full rounded border border-baba-border bg-baba-panel-2 px-2 py-1.5 text-m"
          placeholder={t("add_camera_slug_placeholder")}
        />
      </label>
      <label class="block">
        <span class="block text-s text-baba-text-muted">{t("add_camera_name")}</span>
        <input
          bind:value={form.name}
          required
          class="w-full rounded border border-baba-border bg-baba-panel-2 px-2 py-1.5 text-m"
          placeholder={t("add_camera_name_placeholder")}
        />
      </label>
    </div>

    <StreamUrlField
      streamUrl={form.stream_url}
      substreamUrl={form.substream_url ?? null}
      analysisStream={form.analysis_stream ?? "main"}
      onChange={(v) => Object.assign(form, v)}
      disabled={formBusy}
    />

    <div class="grid grid-cols-2 gap-3 md:grid-cols-4">
      <label class="block">
        <span class="block text-s text-baba-text-muted">{t("add_camera_target_fps")}</span>
        <input
          type="number" min="1" max="30"
          bind:value={form.target_fps}
          class="w-full rounded border border-baba-border bg-baba-panel-2 px-2 py-1.5 text-m"
        />
      </label>
      <label class="block">
        <span class="block text-s text-baba-text-muted">{t("add_camera_max_edge")}</span>
        <input
          type="number" min="0"
          bind:value={form.downscale_max_edge}
          class="w-full rounded border border-baba-border bg-baba-panel-2 px-2 py-1.5 text-m"
        />
      </label>
      <div class="flex items-end gap-3 pb-1">
        <label class="flex items-center gap-2 text-s text-baba-text-muted">
          <input type="checkbox" bind:checked={form.recording_enabled} class="accent-baba-accent" />
          {t("camera_recording_enabled")}
        </label>
      </div>
    </div>

    {#if formError}<p class="text-m text-red-400">{formError}</p>{/if}
    <Button tone="primary" type="submit" disabled={formBusy}>{formBusy ? t("add_camera_submitting") : t("add_camera_submit")}</Button>
  </form>
</section>
{/if}
