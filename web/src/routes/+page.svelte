<script lang="ts">
  import { onMount } from "svelte";
  import {
    api,
    thumbnailUrl,
    type Camera,
    type Sighting,
    type Stats as SystemStats,
  } from "$lib/api";
  import { formatBytes, formatNumber, Stats } from "$lib/kit";
  import { t } from "$lib/i18n";
  import { dt } from "$lib/datetime.svelte";

  // --- state ---

  let stats = $state<SystemStats | null>(null);
  let cameras = $state<Camera[]>([]);
  let recent = $state<Sighting[]>([]);
  let loading = $state(true);
  let error = $state<string | null>(null);

  onMount(async () => {
    try {
      // Three calls in parallel: the dashboard hits API hard at first
      // paint, sequential would add ~150-300 ms over the wire.
      const [s, cs, sg] = await Promise.all([
        api.getStats(),
        api.listCameras(),
        // The SAME visit aggregation Activity uses (not raw track_finalized
        // events): one entry per visit, carrying the resolved identity —
        // "Marko · Patio", not three overlapping "person · Patio" fragments.
        api.listSightings({ limit: 8 }),
      ]);
      stats = s;
      cameras = cs;
      recent = sg;
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      loading = false;
    }
  });

  // Active-in-last-24h per camera comes from stats (cameras with any event
  // in the last 24h) — NOT the 8-item recent feed, which would wrongly mark
  // a camera idle just because it isn't among the latest few sightings.
  const activeCameraIds = $derived(new Set(stats?.active_camera_ids_24h ?? []));

</script>

<div class="space-y-6">

  {#if loading}
    <p class="text-m text-baba-text-muted">{t("dashboard_loading")}</p>
  {:else if error}
    <p class="text-m text-red-400">{t("cameras_error_prefix")}: {error}</p>
  {:else if stats}
    <!-- Onboarding banner: shown only on a fresh install (no cameras
         yet). One link, no dismiss state — once cameras land it
         disappears on its own. -->
    {#if stats.cameras_total === 0}
      <a
        href="/onboarding"
        class="flex items-center justify-between gap-4 rounded-lg border border-baba-accent/40 bg-baba-accent/10 px-4 py-3 hover:bg-baba-accent/15"
      >
        <div>
          <div class="font-medium">{t("onboarding_title")}</div>
          <div class="text-m text-baba-text-muted">{t("onboarding_subtitle")}</div>
        </div>
        <span class="text-baba-accent">→</span>
      </a>
    {/if}

    <Stats stats={[
      {
        label: t("dashboard_metric_cameras"),
        value: stats.cameras_total,
        href: "/settings/cameras",
        detail: `${formatNumber(stats.cameras_enabled)} ${t("dashboard_metric_cameras_enabled")} · ${formatNumber(stats.cameras_active_24h)} ${t("dashboard_metric_cameras_active")}`,
      },
      {
        label: t("dashboard_metric_events_24h"),
        value: stats.events_24h,
        href: "/activity",
        detail: `${formatNumber(stats.events_total)} ${t("dashboard_metric_events_total")}`,
      },
      {
        label: t("dashboard_metric_identities"),
        value: stats.identities_total,
        href: "/identities",
        detail: `${formatNumber(stats.identities_labeled)} ${t("dashboard_metric_identities_labeled")}`,
      },
      {
        label: t("dashboard_metric_storage_used"),
        value: formatBytes(stats.recordings_bytes),
        href: "/settings/storage",
        detail: `${formatNumber(stats.recordings_total)} ${t("dashboard_metric_storage_segments")}`,
      },
    ]} />

    <!-- Two-column main grid: recent events + camera status -->
    <div class="grid gap-4 lg:grid-cols-2">
      <section class="rounded-lg border border-baba-border bg-baba-panel">
        <header class="flex items-center justify-between border-b border-baba-border px-4 py-2">
          <h3 class="text-m font-medium">{t("dashboard_section_recent_events")}</h3>
          <a href="/activity" class="text-s text-baba-accent hover:underline">
            {t("dashboard_open_events")}
          </a>
        </header>
        {#if recent.length === 0}
          <p class="px-4 py-6 text-m text-baba-text-faint">
            {t("dashboard_no_recent_events")}
          </p>
        {:else}
          <ul class="divide-y divide-baba-border">
            {#each recent as s (s.id)}
              <!-- Deep-link into Activity at this camera + moment: Activity reads
                   ?cam & ?t and auto-cuts a clip there (see activity/+page.svelte). -->
              <li>
                <a
                  href={`/activity?cam=${s.camera.id}&t=${encodeURIComponent(s.started_at)}`}
                  class="flex items-center gap-3 px-4 py-2 hover:bg-baba-panel-2"
                >
                  {#if s.thumbnail_path}
                    <img
                      src={thumbnailUrl(s.thumbnail_path)}
                      alt=""
                      loading="lazy"
                      class="h-10 w-14 flex-shrink-0 rounded object-cover bg-black"
                    />
                  {:else}
                    <div class="h-10 w-14 flex-shrink-0 rounded bg-baba-panel-2"></div>
                  {/if}
                  <div class="min-w-0 flex-1">
                    <div class="truncate text-m">
                      {#if s.identity}
                        <span class="font-medium text-baba-accent">{s.identity.name}</span>
                        <span class="text-baba-text-muted">· {s.camera.name}</span>
                      {:else}
                        {s.class_name} · {s.camera.name}
                      {/if}
                    </div>
                    <div class="text-s text-baba-text-faint">{dt.short(s.started_at)}</div>
                  </div>
                </a>
              </li>
            {/each}
          </ul>
        {/if}
      </section>

      <section class="rounded-lg border border-baba-border bg-baba-panel">
        <header class="flex items-center justify-between border-b border-baba-border px-4 py-2">
          <h3 class="text-m font-medium">{t("dashboard_section_cameras")}</h3>
          <a href="/live" class="text-s text-baba-accent hover:underline">
            {t("dashboard_open_live")}
          </a>
        </header>
        {#if cameras.length === 0}
          <p class="px-4 py-6 text-m text-baba-text-faint">{t("live_no_cameras")}</p>
        {:else}
          <ul class="divide-y divide-baba-border">
            {#each cameras as cam (cam.id)}
              {@const active = activeCameraIds.has(cam.id)}
              <li class="flex items-center justify-between gap-3 px-4 py-2">
                <a href={`/live/${cam.slug}`} class="min-w-0 flex-1 truncate text-m hover:underline">
                  {cam.name}
                </a>
                <span
                  class="text-s"
                  class:text-emerald-400={cam.enabled && active}
                  class:text-baba-text-muted={cam.enabled && !active}
                  class:text-red-400={!cam.enabled}
                >
                  {#if !cam.enabled}
                    {t("dashboard_camera_disabled")}
                  {:else if active}
                    ● {t("dashboard_metric_cameras_active")}
                  {:else}
                    {t("dashboard_camera_inactive")}
                  {/if}
                </span>
              </li>
            {/each}
          </ul>
        {/if}
      </section>
    </div>
  {/if}
</div>
