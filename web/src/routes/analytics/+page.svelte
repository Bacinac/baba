<script lang="ts">
  import { byLabel } from "$lib/order";
  import { onMount, untrack } from "svelte";
  import {
    api,
    type Camera,
    type EventsByCameraOut,
    type EventsByHourOut,
    type EventsByKindOut, type ParkingEpisode,
    type HeatmapClassGroup,
    type HeatmapOut,
  } from "$lib/api";
  import { formatNumber, Picks, Stats, Tag, PageHead } from "$lib/kit";
  import { t, type MessageKey } from "$lib/i18n";
  import { formatUptime } from "$lib/format";
  import { dt } from "$lib/datetime.svelte";

  let days = $state<number>(7);
  let loading = $state(true);
  let error = $state<string | null>(null);

  let byHour = $state<EventsByHourOut | null>(null);
  let byCamera = $state<EventsByCameraOut | null>(null);
  let byKind = $state<EventsByKindOut | null>(null);
  let parking = $state<ParkingEpisode[]>([]);

  // Heatmap: separate camera picker (independent of the 3 charts'
  // window — same `days` value for consistency though). Cameras list
  // fetched once on mount; heatmap re-fetched when camera or window
  // changes.
  let cameras = $state<Camera[]>([]);
  let heatmapCameraId = $state<string>("");
  let heatmapClassGroup = $state<HeatmapClassGroup>("all");
  let heatmap = $state<HeatmapOut | null>(null);
  let heatmapLoading = $state(false);
  // Cache-buster for the background snapshot behind the heatmap. Bumped on
  // every heatmap (re)load so the frame refreshes on any selection change —
  // the old code keyed it on `days` alone, so it went stale forever per
  // (camera, days) and never updated on a class-group switch.
  let snapshotBust = $state(0);

  // Race guards — toggling `days` quickly fired overlapping fetches with no
  // ordering guarantee, so a slow old response could clobber a newer one.
  let loadSeq = 0;
  let heatmapSeq = 0;

  async function load() {
    const myseq = ++loadSeq;
    loading = true;
    error = null;
    try {
      const [h, c, k, pk] = await Promise.all([
        api.getEventsByHour({ days }),
        api.getEventsByCamera(days),
        api.getEventsByKind(days),
        api.getParkingHistory(days).catch(() => [] as ParkingEpisode[]),
      ]);
      if (myseq !== loadSeq) return;
      byHour = h;
      byCamera = c;
      byKind = k;
      parking = pk;
    } catch (e) {
      if (myseq === loadSeq) error = e instanceof Error ? e.message : String(e);
    } finally {
      if (myseq === loadSeq) loading = false;
    }
  }

  async function loadCameras() {
    try {
      cameras = await api.listCameras();
      // Default-pick the first enabled camera so the heatmap shows
      // something useful on first paint instead of an empty card.
      if (!heatmapCameraId && cameras.length > 0) {
        const first = cameras.find((c) => c.enabled) ?? cameras[0];
        heatmapCameraId = first.id;
      }
    } catch (e) {
      console.warn("cameras list failed", e);
    }
  }

  async function loadHeatmap() {
    if (!heatmapCameraId) {
      heatmap = null;
      return;
    }
    const myseq = ++heatmapSeq;
    snapshotBust++;
    heatmapLoading = true;
    try {
      const h = await api.getHeatmap(heatmapCameraId, days, heatmapClassGroup);
      if (myseq !== heatmapSeq) return;
      heatmap = h;
    } catch (e) {
      console.warn("heatmap fetch failed", e);
      if (myseq === heatmapSeq) heatmap = null;
    } finally {
      if (myseq === heatmapSeq) heatmapLoading = false;
    }
  }

  onMount(() => {
    // load() and loadHeatmap() are owned by the two $effects below (both run
    // once on mount). onMount only fetches the camera list, which also picks
    // the default heatmap camera and thereby triggers the heatmap effect.
    loadCameras();
  });

  // Re-fetch when window changes. The loaders touch other $state (loadSeq,
  // snapshotBust) synchronously; untrack keeps the effect's dependencies to the
  // explicit inputs read above, so a loader can't self-trigger its own effect
  // (snapshotBust++ inside loadHeatmap is a read+write of the same state — that
  // would otherwise loop into effect_update_depth_exceeded).
  $effect(() => {
    days;
    untrack(() => load());
  });

  $effect(() => {
    heatmapCameraId; days; heatmapClassGroup;
    untrack(() => loadHeatmap());
  });

  // Class toggle options for the heatmap. Order matches the i18n
  // labels; 'all' is first as the natural default.
  const HEATMAP_CLASS_OPTIONS: { v: HeatmapClassGroup; k: MessageKey }[] = [
    { v: "all",     k: "analytics_heatmap_class_all" },
    { v: "person",  k: "analytics_heatmap_class_person" },
    { v: "vehicle", k: "analytics_heatmap_class_vehicle" },
    { v: "animal",  k: "analytics_heatmap_class_animal" },
    { v: "other",   k: "analytics_heatmap_class_other" },
  ];

  // --- derived: dense hour series filled with zeros for missing hours ---

  type DenseHour = { iso: string; date: Date; count: number };

  const denseHours = $derived.by((): DenseHour[] => {
    if (!byHour) return [];
    const since = new Date(byHour.since).getTime();
    const until = new Date(byHour.until).getTime();
    const map = new Map<string, number>();
    for (const b of byHour.buckets) {
      // Postgres date_trunc returns the bucket start in the timestamp's
      // tz; since the column is timestamptz it normalises to UTC. We
      // truncate to the hour boundary so map keys collide.
      const d = new Date(b.bucket);
      d.setMinutes(0, 0, 0);
      map.set(d.toISOString(), b.count);
    }
    const out: DenseHour[] = [];
    for (let t = since; t < until; t += 3_600_000) {
      const d = new Date(t);
      const key = d.toISOString();
      out.push({ iso: key, date: d, count: map.get(key) ?? 0 });
    }
    return out;
  });

  const total = $derived(denseHours.reduce((s, h) => s + h.count, 0));
  const avgPerHour = $derived(
    denseHours.length > 0 ? total / denseHours.length : 0,
  );
  const peakHourCount = $derived(
    denseHours.reduce((m, h) => (h.count > m ? h.count : m), 0),
  );

  // --- SVG chart sizing. Hand-rolled to avoid pulling in a chart lib;
  // matches Timeline.svelte's approach of using a viewBox so the SVG
  // scales to its container without per-render width recomputation. ---

  const HOUR_VB_W = 1440;
  const HOUR_VB_H = 180;
  const HOUR_PAD_TOP = 8;
  const HOUR_PAD_BOTTOM = 22;
  const HOUR_PAD_X = 4;
  const HOUR_PLOT_H = HOUR_VB_H - HOUR_PAD_TOP - HOUR_PAD_BOTTOM;

  function hourBarX(i: number, n: number): number {
    if (n === 0) return 0;
    const w = (HOUR_VB_W - HOUR_PAD_X * 2) / n;
    return HOUR_PAD_X + i * w;
  }
  function hourBarW(n: number): number {
    if (n === 0) return 0;
    const w = (HOUR_VB_W - HOUR_PAD_X * 2) / n;
    // Half-pixel inner gap so adjacent bars don't merge into a slab.
    return Math.max(0.5, w - 0.6);
  }
  function hourBarH(count: number): number {
    if (peakHourCount === 0) return 0;
    return (count / peakHourCount) * HOUR_PLOT_H;
  }

  // Camera horizontal bars: proportional to peak.
  const cameraPeak = $derived(
    byCamera ? byCamera.rows.reduce((m, r) => (r.count > m ? r.count : m), 0) : 0,
  );
  function cameraBarPct(count: number): number {
    if (cameraPeak === 0) return 0;
    return Math.max(0, (count / cameraPeak) * 100);
  }

  const kindLabel = (kind: string) => t(`events_kind_${kind}` as MessageKey);

  // --- heatmap colour mapping ---
  //
  // Normalised count (0..1) → rgba string. Sub-1% counts get full
  // transparency so empty / near-empty cells don't muddy the snapshot.
  // The palette goes amber → red for visibility on both dark and light
  // camera frames. Square-root scaling brightens low counts so the
  // operator notices fringes around hot zones, not just the centre.
  function heatColor(rawValue: number, peak: number): string {
    if (peak <= 0 || rawValue <= 0) return "rgba(0,0,0,0)";
    const norm = Math.sqrt(rawValue / peak);
    if (norm < 0.02) return "rgba(0,0,0,0)";
    // Two-stop gradient: amber (low-mid) → red (high). Opacity climbs
    // with norm so peak cells dominate, low-density cells stay subtle.
    const opacity = Math.min(0.85, 0.15 + norm * 0.7);
    if (norm < 0.5) {
      // amber 245,158,11 → orange 251,113,53 (interpolate)
      const t = norm / 0.5;
      const r = Math.round(245 + (251 - 245) * t);
      const g = Math.round(158 + (113 - 158) * t);
      const b = Math.round(11 + (53 - 11) * t);
      return `rgba(${r},${g},${b},${opacity})`;
    }
    // orange → red 239,68,68
    const t = (norm - 0.5) / 0.5;
    const r = Math.round(251 + (239 - 251) * t);
    const g = Math.round(113 + (68 - 113) * t);
    const b = Math.round(53 + (68 - 53) * t);
    return `rgba(${r},${g},${b},${opacity})`;
  }

  // Cell coordinates for the SVG render. We use a 100×100 viewBox so the
  // SVG scales to the parent's aspect-ratio-locked container (we letterbox
  // by setting the wrapper to the same 16:9 the snapshot is in).
  function cellRect(i: number, w: number, h: number) {
    const x = i % w;
    const y = Math.floor(i / w);
    return {
      x: (x / w) * 100,
      y: (y / h) * 100,
      w: 100 / w,
      h: 100 / h,
    };
  }
</script>

<div class="space-y-6">
  <PageHead sticky={false}>
    {#snippet ways()}
      <Picks
        picks={([[1, "analytics_window_24h"], [7, "analytics_window_7d"], [30, "analytics_window_30d"]] as const).map(([v, k]) => ({ key: String(v), label: t(k) }))}
        chosen={[String(days)]}
        onpick={(k) => (days = Number(k))}
      />
    {/snippet}
  </PageHead>

  {#if loading}
    <p class="text-m text-baba-text-muted">{t("analytics_loading")}</p>
  {:else if error}
    <p class="text-m text-red-400">{t("cameras_error_prefix")}: {error}</p>
  {:else}
    <Stats stats={[
      { label: t("analytics_total_events"), value: total },
      { label: t("analytics_avg_per_hour"), value: formatNumber(avgPerHour, { maximumFractionDigits: avgPerHour < 10 ? 1 : 0 }) },
      { label: t("analytics_active_cameras"), value: byCamera?.rows.length ?? 0 },
    ]} />

    <!-- Hourly histogram (SVG) -->
    <section class="rounded-lg border border-baba-border bg-baba-panel">
      <header class="border-b border-baba-border px-4 py-2">
        <h3 class="text-m font-medium">{t("analytics_section_by_hour")}</h3>
      </header>
      <div class="p-4">
        {#if denseHours.length === 0 || peakHourCount === 0}
          <p class="text-m text-baba-text-faint">{t("analytics_empty")}</p>
        {:else}
          <svg
            viewBox="0 0 {HOUR_VB_W} {HOUR_VB_H}"
            preserveAspectRatio="none"
            class="block h-48 w-full"
            role="img"
            aria-label={t("analytics_section_by_hour")}
          >
            {#each denseHours as h, i (h.iso)}
              {@const bh = hourBarH(h.count)}
              <rect
                x={hourBarX(i, denseHours.length)}
                y={HOUR_PAD_TOP + (HOUR_PLOT_H - bh)}
                width={hourBarW(denseHours.length)}
                height={bh}
                fill={h.count > 0 ? "#f59e0b" : "rgba(255,255,255,0.04)"}
              >
                <title>{dt.short(h.iso)} · {h.count}</title>
              </rect>
            {/each}
            <line
              x1={HOUR_PAD_X} y1={HOUR_PAD_TOP + HOUR_PLOT_H}
              x2={HOUR_VB_W - HOUR_PAD_X} y2={HOUR_PAD_TOP + HOUR_PLOT_H}
              stroke="rgba(255,255,255,0.15)" stroke-width="0.5"
            />
          </svg>
          <div class="mt-1 flex justify-between text-2xs text-baba-text-faint">
            <span>{dt.short(denseHours[0].iso)}</span>
            <span>{dt.short(denseHours[denseHours.length - 1].iso)}</span>
          </div>
        {/if}
      </div>
    </section>

    <div class="grid gap-4 lg:grid-cols-2">
      <!-- Busiest cameras -->
      <section class="rounded-lg border border-baba-border bg-baba-panel">
        <header class="border-b border-baba-border px-4 py-2">
          <h3 class="text-m font-medium">{t("analytics_section_by_camera")}</h3>
        </header>
        <ul class="divide-y divide-baba-border">
          {#if !byCamera || byCamera.rows.length === 0}
            <li class="px-4 py-3 text-m text-baba-text-faint">{t("analytics_empty")}</li>
          {:else}
            {#each byCamera.rows as r (r.camera_id)}
              <li class="px-4 py-2">
                <div class="flex items-baseline justify-between text-m">
                  <a href={`/live/${r.slug}`} class="truncate hover:underline">{r.name}</a>
                  <span class="tabular-nums text-baba-text-muted">{formatNumber(r.count, { maximumFractionDigits: 1 })}</span>
                </div>
                <div class="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-baba-panel-2">
                  <div
                    class="h-full bg-baba-accent"
                    style="width: {cameraBarPct(r.count)}%"
                  ></div>
                </div>
              </li>
            {/each}
          {/if}
        </ul>
      </section>

      <!-- By kind -->
      <section class="rounded-lg border border-baba-border bg-baba-panel">
        <header class="border-b border-baba-border px-4 py-2">
          <h3 class="text-m font-medium">{t("analytics_section_by_kind")}</h3>
        </header>
        <ul class="divide-y divide-baba-border">
          {#if !byKind || byKind.rows.length === 0}
            <li class="px-4 py-3 text-m text-baba-text-faint">{t("analytics_empty")}</li>
          {:else}
            {#each byKind.rows as r (r.kind)}
              <li class="flex items-baseline justify-between px-4 py-2 text-m">
                <span class="truncate text-s">{kindLabel(r.kind)}</span>
                <span class="tabular-nums text-baba-text-muted">{formatNumber(r.count, { maximumFractionDigits: 1 })}</span>
              </li>
            {/each}
          {/if}
        </ul>
      </section>
    </div>

    <!-- Movement heatmap -->
    <section class="rounded-lg border border-baba-border bg-baba-panel">
      <header class="flex flex-wrap items-center justify-between gap-3 border-b border-baba-border px-4 py-2">
        <h3 class="text-m font-medium">{t("analytics_section_heatmap")}</h3>
        <div class="flex flex-wrap items-center gap-3">
          <!-- Class group toggle. 'All' sums across groups server-side;
               the rest filter to one group so the operator can see
               where pedestrians vs vehicles actually move. -->
          <Picks
            picks={HEATMAP_CLASS_OPTIONS.map((o) => ({ key: o.v, label: t(o.k) }))}
            chosen={[heatmapClassGroup]}
            onpick={(k) => (heatmapClassGroup = k as typeof heatmapClassGroup)}
          />
          <label class="text-s text-baba-text-faint">
            <span class="block">{t("analytics_heatmap_pick_camera")}</span>
            <select
              bind:value={heatmapCameraId}
              class="rounded border border-baba-border bg-baba-bg px-2 py-1 text-m"
            >
              {#each byLabel(cameras, (c) => c.name) as cam (cam.id)}
                <option value={cam.id}>{cam.name}</option>
              {/each}
            </select>
          </label>
        </div>
      </header>
      <div class="p-4">
        {#if !heatmapCameraId}
          <p class="text-m text-baba-text-faint">{t("live_no_cameras")}</p>
        {:else if heatmapLoading}
          <p class="text-m text-baba-text-muted">{t("analytics_loading")}</p>
        {:else if !heatmap || heatmap.max_count === 0}
          <p class="text-m text-baba-text-faint">{t("analytics_heatmap_empty")}</p>
        {:else}
          <div class="relative w-full overflow-hidden rounded bg-black" style="aspect-ratio: 16 / 9;">
            <!-- svelte-ignore a11y_missing_attribute -->
            <img
              src={api.snapshotUrl(heatmap.camera_id, snapshotBust)}
              class="absolute inset-0 h-full w-full object-cover opacity-60"
            />
            <svg
              class="absolute inset-0 h-full w-full"
              viewBox="0 0 100 100"
              preserveAspectRatio="none"
              role="img"
              aria-label={t("analytics_section_heatmap")}
            >
              {#each heatmap.cells as count, i (i)}
                {@const r = cellRect(i, heatmap.grid_w, heatmap.grid_h)}
                <rect
                  x={r.x} y={r.y} width={r.w} height={r.h}
                  fill={heatColor(count, heatmap.max_count)}
                />
              {/each}
            </svg>
          </div>
          <div class="mt-2 flex items-center justify-between text-s text-baba-text-faint">
            <div class="flex items-center gap-2">
              <span>{t("analytics_heatmap_legend_low")}</span>
              <div
                class="h-2 w-32 rounded"
                style="background: linear-gradient(to right, rgba(245,158,11,0.3), rgba(251,113,53,0.6), rgba(239,68,68,0.85));"
              ></div>
              <span>{t("analytics_heatmap_legend_high")}</span>
            </div>
            <span class="tabular-nums">
              {t("analytics_heatmap_peak")}: {formatNumber(heatmap.max_count, { maximumFractionDigits: 1 })}
            </span>
          </div>
        {/if}
      </div>
    </section>

    <!-- Parking history: the occupancy registry, read straight. One row per
         episode — who stood where, since when, for how long, on what
         evidence. "Nepoznato vozilo" is a real record, not a gap. -->
    {#if parking.length > 0}
      <section class="rounded-lg border border-baba-border bg-baba-panel">
        <header class="border-b border-baba-border px-4 py-2">
          <h3 class="text-m font-medium">{t("analytics_section_parking")}</h3>
        </header>
        <ul class="divide-y divide-baba-border">
          {#each parking as ep (ep.place + ep.occupied_since)}
            <li class="flex items-center gap-3 px-4 py-2 text-m">
              <span class="w-10 shrink-0 font-mono text-baba-accent">{ep.place}</span>
              <span class="min-w-0 truncate {ep.name ? 'text-baba-text' : 'text-baba-text-muted'}">
                {ep.name ?? t("analytics_parking_unknown")}
              </span>
              <span class="tabular-nums text-baba-text-muted">
                {dt.short(ep.occupied_since)}{ep.released_at ? ` – ${dt.hm(ep.released_at)}` : ""}
              </span>
              <span class="tabular-nums text-baba-text-faint">{formatUptime(ep.duration_s)}</span>
              {#if !ep.released_at}
                <Tag tone="ok">
                  {t("identities_presence_now")}
                </Tag>
              {/if}
              <span class="ml-auto"><Tag tone={ep.evidence === "plate" ? "busy" : ep.evidence === "unknown" ? "quiet" : "fact"}>
                {ep.evidence === 'plate'
                   ? t("analytics_evidence_plate")
                   : ep.evidence === 'unknown'
                     ? t("analytics_evidence_none")
                     : t("identities_evidence_body")}
              </Tag></span>
            </li>
          {/each}
        </ul>
      </section>
    {/if}
  {/if}
</div>
