<script lang="ts">
  import { formatNumber } from "$lib/kit";
  import { t } from "$lib/i18n";
  import { onMount, onDestroy } from "svelte";
  import { api, type Camera, type LiveStatus, type ParkedVehicle } from "$lib/api";
  import { liveOverlay } from "$lib/liveOverlay.svelte";
  import LiveTile from "$lib/LiveTile.svelte";

  let cameras = $state<Camera[]>([]);
  let loading = $state(true);
  let error = $state<string | null>(null);
  // slug → live status (measured fps + current detections for the badges)
  let status = $state<Record<string, LiveStatus>>({});
  let parked = $state<ParkedVehicle[]>([]);
  let pollTimer: ReturnType<typeof setInterval> | null = null;
  let parkedFetchedAt = 0;

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

  async function pollStatus() {
    try {
      status = await api.getLiveStatus();
      if (Date.now() - parkedFetchedAt > 30_000) {
        parkedFetchedAt = Date.now();
        parked = await api.getParkedVehicles();
      }
    } catch {
      /* transient — keep last */
    }
  }

  onMount(async () => {
    await Promise.all([refresh(), liveOverlay.load()]);
    await pollStatus();
    // 1 s cadence: drives both the measured-fps label and the count chips.
    pollTimer = setInterval(pollStatus, 1000);
  });
  onDestroy(() => {
    if (pollTimer !== null) clearInterval(pollTimer);
  });
</script>

{#if loading}
  <p class="text-baba-text-faint">{t("cameras_loading")}</p>
{:else if error}
  <p class="text-red-400">{error}</p>
{:else if cameras.length === 0}
  <p class="text-baba-text-faint">{t("live_no_cameras")}</p>
{:else}
  <ul class="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
    {#each cameras as cam (cam.id)}
      <li class="rounded-lg border border-baba-border bg-baba-panel p-3">
        <a class="block" href={`/live/${cam.slug}`}>
          <!-- Grid tile = a still of the detector's downscaled SHM frame,
               re-asked every second. Image decode, never a video-decode
               context, so every camera renders at once without exhausting the
               browser's decoders — and, unlike the Motion-JPEG stream this
               replaced, a tile that stops updating says so instead of showing
               an old frame as live. The full MSE video lives on the per-camera
               detail view. -->
          <LiveTile cameraId={cam.id} name={cam.name} boxes={liveOverlay.boxes}
                    parked={parked.filter((p) => p.camera_id === cam.id)} />
          <div class="font-medium">{cam.name}</div>
          <div class="text-s text-baba-text-faint">
            <!-- Measured effective rate (adaptive), not the configured target:
                 0 when the camera's quiet, ramps up on activity. -->
            {formatNumber(status[cam.slug]?.fps ?? 0, { minimumFractionDigits: 1, maximumFractionDigits: 1 })} {t("cameras_fps_suffix")}
          </div>
        </a>
      </li>
    {/each}
  </ul>
{/if}
