<script lang="ts">
  import { onMount } from "svelte";
  import {
    api,
    type Stats,
    type SystemInfo,
    type SystemMetrics,
    type ServiceMetrics,
    type ServiceTiming,
  } from "$lib/api";
  import { formatNumber } from "$lib/kit";
  import { t } from "$lib/i18n";
  import { formatMetric, formatUptime } from "$lib/format";
  // A service that is genuinely broken keeps erroring, so the line stays.
  // One error an hour ago is history, and a card left permanently red is
  // wallpaper — the panel has to mean something when it is red.
  const ERROR_SHELF_LIFE_S = 3600;
  import TelemetryIncidents from "$lib/TelemetryIncidents.svelte";

  let info = $state<SystemInfo | null>(null);
  let stats = $state<Stats | null>(null);
  let metrics = $state<SystemMetrics | null>(null);
  let loading = $state(true);
  let error = $state<string | null>(null);

  async function loadInfo() {
    try {
      const [si, st] = await Promise.all([api.getSystemInfo(), api.getStats()]);
      info = si;
      stats = st;
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      loading = false;
    }
  }

  // Live pipeline stats — polled at roughly the services' publish cadence.
  // Best-effort: a transient fetch error keeps the last good snapshot
  // rather than blanking the panel.
  async function loadMetrics() {
    try {
      metrics = await api.getSystemMetrics();
    } catch {
      /* keep last snapshot */
    }
  }

  onMount(() => {
    loadInfo();
    loadMetrics();
    const id = setInterval(loadMetrics, 5000);
    return () => clearInterval(id);
  });

  // The busiest rate becomes the card's headline series + sparkline.
  function primaryRate(svc: ServiceMetrics): string | null {
    const keys = Object.keys(svc.rates);
    if (keys.length === 0) return null;
    return keys.reduce((best, k) => (svc.rates[k] > svc.rates[best] ? k : best), keys[0]);
  }

  // SVG polyline path (viewBox 100×24) over a rate's rolling history.
  function sparkline(svc: ServiceMetrics, key: string): string {
    const pts = svc.history.map((h) => h.rates[key] ?? 0);
    if (pts.length < 2) return "";
    const max = Math.max(...pts, 1e-6);
    const n = pts.length;
    return pts
      .map((v, i) => {
        const x = (i / (n - 1)) * 100;
        const y = 23 - (v / max) * 22;
        return `${i === 0 ? "M" : "L"}${Math.round(x * 10) / 10},${Math.round(y * 10) / 10}`;
      })
      .join(" ");
  }

  // Show the dev-default cors lookup positively (= using dev defaults).
  // Production deployments override BABA_CORS_ORIGINS to their public origins.
  const corsIsDevDefault = $derived(
    info?.cors_origins.every((o) =>
      ["http://localhost:5173", "http://127.0.0.1:5173"].includes(o),
    ) ?? false,
  );

  // Host hardware telemetry arrives as a "hardware" pseudo-service on
  // baba.stats.*; render it in its own section, not among the pipeline stages.
  const hwSvc = $derived(metrics?.services.find((s) => s.service === "hardware") ?? null);
  const pipelineServices = $derived(
    metrics?.services.filter((s) => s.service !== "hardware") ?? [],
  );

  function timing(service: string, metric: string): ServiceTiming | null {
    return metrics?.services.find((s) => s.service === service)?.timings[metric] ?? null;
  }

  const detectorLatency = $derived(timing("detector", "infer_ms"));
  const embedderLatency = $derived(timing("embedder", "embed_ms"));

  function pctColor(pct: number | null): string {
    if (pct === null) return "bg-sky-500";
    if (pct >= 95) return "bg-red-500";
    if (pct >= 80) return "bg-amber-500";
    return "bg-emerald-500";
  }

  function gb(mb: number | null): string {
    return mb === null ? "—" : formatMetric(mb / 1024);
  }

  // Flatten the hardware gauges into a typed view (Svelte {@const} can't live
  // directly under a plain element, so we resolve everything here).
  // Display names + stable order for the per-engine GPU gauges the collector
  // emits as gpu_eng_<key>_pct. Intel (i915 PMU) splits five engines; NVIDIA
  // reports compute/encode/decode. Unknown keys fall back to the raw key.
  const ENGINE_LABELS: Record<string, string> = {
    compute: "Compute",
    video: "Video",
    videoenhance: "VideoEnhance",
    render3d: "Render/3D",
    blitter: "Blitter",
    encode: "Encode",
    decode: "Decode",
  };
  const ENGINE_ORDER = [
    "compute",
    "video",
    "videoenhance",
    "render3d",
    "blitter",
    "encode",
    "decode",
  ];

  const hw = $derived.by(() => {
    const s = hwSvc;
    if (!s) return null;
    const g = (k: string): number | null => {
      const v = s.gauges[k];
      return typeof v === "number" && isFinite(v) ? v : null;
    };
    const engines = Object.keys(s.gauges)
      .filter((k) => k.startsWith("gpu_eng_") && k.endsWith("_pct"))
      .map((k) => {
        const key = k.slice("gpu_eng_".length, -"_pct".length);
        return { key, label: ENGINE_LABELS[key] ?? key, pct: g(k) };
      })
      .filter((e): e is { key: string; label: string; pct: number } => e.pct !== null)
      .sort((a, b) => {
        const ia = ENGINE_ORDER.indexOf(a.key);
        const ib = ENGINE_ORDER.indexOf(b.key);
        return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
      });
    const memUsed = g("gpu_mem_used_mb");
    const memTotal = g("gpu_mem_total_mb");
    return {
      stale: s.stale,
      cpuPct: g("cpu_pct"),
      cores: g("cpu_cores"),
      load1: g("cpu_load_1m"),
      load5: g("cpu_load_5m"),
      load15: g("cpu_load_15m"),
      ramPct: g("ram_pct"),
      ramUsed: g("ram_used_mb"),
      ramTotal: g("ram_total_mb"),
      engines,
      hasGpu: engines.length > 0,
      gpuFreq: g("gpu_freq_mhz"),
      gpuFreqReq: g("gpu_freq_req_mhz"),
      gpuRc6: g("gpu_rc6_pct"),
      gpuBwR: g("gpu_membw_read_mibs"),
      gpuBwW: g("gpu_membw_write_mibs"),
      gpuTemp: g("gpu_temp_c"),
      gpuMemUsed: memUsed,
      gpuMemTotal: memTotal,
      gpuMemPct: memUsed !== null && memTotal ? (100 * memUsed) / memTotal : null,
      // Vendor-neutral extras: whichever collector can read them fills them,
      // and each renders only when present — so the Intel panel is unchanged
      // until its collector learns the same fields.
      gpuFreqMax: g("gpu_freq_max_mhz"),
      gpuPower: g("gpu_power_w"),
      gpuPowerLimit: g("gpu_power_limit_w"),
      gpuFan: g("gpu_fan_pct"),
      gpuClockMem: g("gpu_clock_mem_mhz"),
      gpuMemIo: g("gpu_mem_io_pct"),
      gpuPcieGen: g("gpu_pcie_gen"),
      gpuPcieWidth: g("gpu_pcie_width"),
      gpuThrottleThermal: g("gpu_throttle_thermal") === 1,
      gpuThrottlePower: g("gpu_throttle_power") === 1,
    };
  });
</script>

<div class="space-y-6">

  <!-- Live pipeline health — one card per service, fed by baba.stats.*
       (no Prometheus/Grafana; in-app at single-box scale). Polls every 5s. -->
  {#if pipelineServices.length > 0}
    <section class="rounded-lg border border-baba-border bg-baba-panel">
      <header class="flex items-baseline justify-between border-b border-baba-border px-4 py-2">
        <h3 class="text-m font-medium">{t("system_section_pipeline")}</h3>
        <span class="text-s text-baba-text-faint">{t("system_pipeline_live")}</span>
      </header>
      <div class="grid gap-3 p-3 sm:grid-cols-2 xl:grid-cols-3">
        {#each pipelineServices as svc (svc.service)}
          {@const pk = primaryRate(svc)}
          <div class="rounded-md border border-baba-border bg-baba-panel-2 p-3">
            <div class="flex items-center justify-between">
              <div class="flex items-center gap-2">
                <span class={svc.stale ? "text-amber-400" : "text-emerald-400"}>●</span>
                <span class="font-medium">{svc.service}</span>
              </div>
              <span class="text-s text-baba-text-faint">
                {svc.stale ? t("system_pipeline_stale") : t("system_pipeline_up")}
                · {formatUptime(svc.uptime_s)}
              </span>
            </div>

            {#if pk}
              <div class="mt-2 flex items-end justify-between gap-2">
                <div class="text-2xl font-semibold tabular-nums">
                  {formatMetric(svc.rates[pk])}<span class="ml-1 text-s font-normal text-baba-text-muted">{pk}/s</span>
                </div>
                <svg viewBox="0 0 100 24" preserveAspectRatio="none" class="h-8 w-24 text-sky-400">
                  <path d={sparkline(svc, pk)} fill="none" stroke="currentColor" stroke-width="1.5" />
                </svg>
              </div>
            {/if}

            <div class="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-s text-baba-text-muted">
              {#each Object.entries(svc.rates) as [k, v]}
                {#if k !== pk}
                  <span><span class="text-baba-text-faint">{k}</span> {formatMetric(v)}/s</span>
                {/if}
              {/each}
              {#each Object.entries(svc.gauges) as [k, v]}
                <span><span class="text-baba-text-faint">{k}</span> {formatMetric(v)}</span>
              {/each}
              {#each Object.entries(svc.timings) as [k, t]}
                <span><span class="text-baba-text-faint">{k}</span> {formatMetric(t.p50)}/{formatMetric(t.p95)}ms</span>
              {/each}
            </div>

            <!-- Lifetime totals (svc.counters). The rates above are per-second;
                 these are the cumulative counts since the service started, which
                 the page previously dropped entirely. -->
            {#if Object.keys(svc.counters).length}
              <div class="mt-1 flex flex-wrap items-baseline gap-x-3 gap-y-0.5 text-2xs text-baba-text-faint">
                <span title={t("system_lifetime_totals")}>Σ</span>
                {#each Object.entries(svc.counters) as [k, v]}
                  <span>{k} {formatNumber(v, { maximumFractionDigits: 1 })}</span>
                {/each}
              </div>
            {/if}

            {#if svc.last_error && (svc.last_error_age_s ?? 0) < ERROR_SHELF_LIFE_S}
              <div class="mt-2 truncate text-s text-red-400" title={svc.last_error}>
                ⚠ {svc.last_error}
                {#if svc.last_error_age_s}
                  <span class="text-baba-text-faint"
                    >· {t("system_last_error_ago")}
                    {formatUptime(svc.last_error_age_s)}</span
                  >
                {/if}
              </div>
            {/if}
          </div>
        {/each}
      </div>
    </section>
  {/if}

  <!-- Host hardware — CPU / RAM / GPU, from the vendor-agnostic hwstats
       collector (nvidia-smi / intel_gpu_top). Bars go amber >80%, red >95%. -->
  {#if hw}
    <section class="rounded-lg border border-baba-border bg-baba-panel">
      <header class="flex items-baseline justify-between border-b border-baba-border px-4 py-2">
        <h3 class="text-m font-medium">{t("system_section_hardware")}</h3>
        <span class="text-s {hw.stale ? 'text-amber-400' : 'text-baba-text-faint'}">
          {hw.stale ? t("system_pipeline_stale") : t("system_pipeline_live")}
        </span>
      </header>
      <div class="space-y-5 p-4">
        <div class="grid gap-5 sm:grid-cols-2">
          <!-- CPU -->
          <div class="space-y-1.5">
            <div class="flex items-baseline justify-between">
              <span class="text-m font-medium">{t("hw_cpu")}</span>
              <span class="text-s text-baba-text-muted">
                {#if hw.cores !== null}{formatMetric(hw.cores)} {t("hw_cores")}{/if}
              </span>
            </div>
            <div class="h-2 w-full overflow-hidden rounded bg-baba-panel-2">
              <div class="h-full {pctColor(hw.cpuPct)}" style="width: {Math.min(100, hw.cpuPct ?? 0)}%"></div>
            </div>
            <div class="flex justify-between text-s tabular-nums text-baba-text-muted">
              <span>{hw.cpuPct === null ? "—" : formatMetric(hw.cpuPct) + "%"}</span>
              <span class="text-baba-text-faint">
                {t("hw_load")}
                {hw.load1 === null ? "—" : formatMetric(hw.load1)} ·
                {hw.load5 === null ? "—" : formatMetric(hw.load5)} ·
                {hw.load15 === null ? "—" : formatMetric(hw.load15)}
              </span>
            </div>
          </div>

          <!-- RAM -->
          <div class="space-y-1.5">
            <div class="flex items-baseline justify-between">
              <span class="text-m font-medium">{t("hw_ram")}</span>
              <span class="text-s text-baba-text-muted">{gb(hw.ramUsed)} / {gb(hw.ramTotal)} GB</span>
            </div>
            <div class="h-2 w-full overflow-hidden rounded bg-baba-panel-2">
              <div class="h-full {pctColor(hw.ramPct)}" style="width: {Math.min(100, hw.ramPct ?? 0)}%"></div>
            </div>
            <div class="text-s tabular-nums text-baba-text-muted">
              {hw.ramPct === null ? "—" : formatMetric(hw.ramPct) + "%"}
            </div>
          </div>
        </div>

        <!-- GPU: full per-engine breakdown + device metrics -->
        {#if hw.hasGpu}
          <div class="space-y-2 rounded-md border border-baba-border bg-baba-panel-2/40 p-3">
            <div class="flex items-baseline justify-between">
              <span class="text-m font-medium">
                {t("hw_gpu")}{#if info?.gpu_device} · {info.gpu_device}{:else if info?.variant} · {info.variant === "intel"
                    ? "Intel"
                    : info.variant === "nvidia"
                      ? "NVIDIA"
                      : info.variant}{/if}
              </span>
              <span class="text-s tabular-nums text-baba-text-muted">
                {#if hw.gpuFreq !== null}{formatMetric(hw.gpuFreq)}{#if hw.gpuFreqReq !== null}/{formatMetric(
                      hw.gpuFreqReq,
                    )}{:else if hw.gpuFreqMax !== null}/{formatMetric(
                      hw.gpuFreqMax,
                    )}{/if} MHz{/if}{#if hw.gpuTemp !== null} · {formatMetric(hw.gpuTemp)}°C{/if}{#if hw.gpuPower !== null} · {formatMetric(hw.gpuPower)}{#if hw.gpuPowerLimit !== null}/{formatMetric(
                      hw.gpuPowerLimit,
                    )}{/if} W{/if}{#if hw.gpuMemPct !== null} · {t("hw_vram")} {gb(hw.gpuMemUsed)} / {gb(hw.gpuMemTotal)} GB{/if}
              </span>
            </div>

            <!-- one bar per GPU engine -->
            <div class="space-y-1.5">
              {#each hw.engines as e (e.key)}
                <div class="flex items-center gap-2">
                  <span class="w-28 shrink-0 truncate text-s text-baba-text-muted">{e.label}</span>
                  <div class="h-2 flex-1 overflow-hidden rounded bg-baba-panel-2">
                    <div class="h-full {pctColor(e.pct)}" style="width: {Math.min(100, e.pct)}%"></div>
                  </div>
                  <span class="w-12 shrink-0 text-right text-s tabular-nums text-baba-text-muted">
                    {formatMetric(e.pct)}%
                  </span>
                </div>
              {/each}

              <!-- VRAM (when the tool reports it, e.g. nvidia-smi) -->
              {#if hw.gpuMemPct !== null}
                <div class="flex items-center gap-2">
                  <span class="w-28 shrink-0 truncate text-s text-baba-text-muted">{t("hw_vram")}</span>
                  <div class="h-2 flex-1 overflow-hidden rounded bg-baba-panel-2">
                    <div class="h-full {pctColor(hw.gpuMemPct)}" style="width: {Math.min(100, hw.gpuMemPct)}%"></div>
                  </div>
                  <span class="w-12 shrink-0 text-right text-s tabular-nums text-baba-text-muted">
                    {formatMetric(hw.gpuMemPct)}%
                  </span>
                </div>
              {/if}
            </div>

            <!-- device metrics footer — whatever this card reports. Intel fills
                 rc6 idle + mem bandwidth, NVIDIA fan / memory I/O / PCIe link.
                 Guarded as a whole so a card that reports none of it doesn't
                 render an empty bordered strip. VRAM GB is on the header line. -->
            {#if hw.gpuRc6 !== null || hw.gpuBwR !== null || hw.gpuBwW !== null || hw.gpuFan !== null || hw.gpuMemIo !== null || hw.gpuClockMem !== null || hw.gpuPcieGen !== null || hw.gpuThrottleThermal || hw.gpuThrottlePower}
              <div class="flex flex-wrap gap-x-4 gap-y-1 border-t border-baba-border pt-2 text-s tabular-nums text-baba-text-faint">
                {#if hw.gpuRc6 !== null}<span>{t("hw_idle")} {formatMetric(hw.gpuRc6)}%</span>{/if}
                {#if hw.gpuBwR !== null || hw.gpuBwW !== null}
                  <span>
                    {t("hw_membw")}
                    {hw.gpuBwR === null ? "—" : formatMetric(hw.gpuBwR)} / {hw.gpuBwW === null ? "—" : formatMetric(hw.gpuBwW)} MiB/s
                  </span>
                {/if}
                {#if hw.gpuFan !== null}<span>{t("hw_fan")} {formatMetric(hw.gpuFan)}%</span>{/if}
                {#if hw.gpuMemIo !== null}<span>{t("hw_mem_io")} {formatMetric(hw.gpuMemIo)}%</span>{/if}
                {#if hw.gpuClockMem !== null}<span>{t("hw_mem_clock")} {formatMetric(hw.gpuClockMem)} MHz</span>{/if}
                {#if hw.gpuPcieGen !== null}
                  <span>
                    PCIe {formatMetric(hw.gpuPcieGen)}.0{#if hw.gpuPcieWidth !== null} ×{formatMetric(hw.gpuPcieWidth)}{/if}
                  </span>
                {/if}
                <!-- Only when actually throttling: a permanent "not throttling"
                     badge is noise, and this must read as a fault when it shows. -->
                {#if hw.gpuThrottleThermal || hw.gpuThrottlePower}
                  <span class="font-medium text-red-400">
                    {t("hw_throttling")}
                    {hw.gpuThrottleThermal ? t("hw_throttle_thermal") : ""}{hw.gpuThrottleThermal &&
                    hw.gpuThrottlePower
                      ? " + "
                      : ""}{hw.gpuThrottlePower ? t("hw_throttle_power") : ""}
                  </span>
                {/if}
              </div>
            {/if}
          </div>
        {/if}
      </div>
    </section>
  {/if}

  <!-- Watcher incidents over the telemetry flight recorder: AI verdicts,
       apply/dismiss, auto-analyze switch. Below the hardware gauges per
       operator preference — health numbers first, exceptions after. -->
  <TelemetryIncidents />

  {#if loading}
    <p class="text-m text-baba-text-muted">{t("system_loading")}</p>
  {:else if error}
    <p class="text-m text-red-400">{t("cameras_error_prefix")}: {error}</p>
  {:else if info && stats}
    <!-- Two-column grid: runtime + inference on left, security + db on right -->
    <div class="grid gap-4 lg:grid-cols-2">
      <section class="rounded-lg border border-baba-border bg-baba-panel">
        <header class="border-b border-baba-border px-4 py-2">
          <h3 class="text-m font-medium">{t("system_section_runtime")}</h3>
        </header>
        <dl class="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-2 px-4 py-3 text-m">
          <dt class="text-baba-text-muted">{t("system_field_api_version")}</dt>
          <dd class="font-mono">{info.api_version}</dd>
          <dt class="text-baba-text-muted">{t("system_field_python")}</dt>
          <dd class="font-mono">{info.python_version}</dd>
          <dt class="text-baba-text-muted">{t("system_field_variant")}</dt>
          <dd class="font-mono">{info.variant}</dd>
        </dl>
      </section>

      <section class="rounded-lg border border-baba-border bg-baba-panel">
        <header class="border-b border-baba-border px-4 py-2">
          <h3 class="text-m font-medium">{t("system_section_inference")}</h3>
        </header>
        <dl class="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-2 px-4 py-3 text-m">
          <dt class="text-baba-text-muted">{t("system_field_detector_model")}</dt>
          <dd class="break-all font-mono text-s">{info.detector_model || "—"}</dd>
          <dt class="text-baba-text-muted">{t("system_field_detector_family")}</dt>
          <dd class="font-mono text-s">{info.detector_family || "—"}</dd>
          <dt class="text-baba-text-muted">{t("system_field_detector_latency")}</dt>
          <dd>
            {#if detectorLatency}
              <span class="font-mono tabular-nums"
                >{formatMetric(detectorLatency.p50)} / {formatMetric(detectorLatency.p95)} ms</span
              >
              <span class="ml-1 text-s text-baba-text-faint">p50 / p95</span>
            {:else}
              <span class="text-baba-text-faint">—</span>
            {/if}
          </dd>
          <dt class="text-baba-text-muted">{t("system_field_embedder_model")}</dt>
          <dd class="break-all font-mono text-s">{info.embedder_model || "—"}</dd>
          <dt class="text-baba-text-muted">{t("system_field_embedder_latency")}</dt>
          <dd>
            {#if embedderLatency}
              <span class="font-mono tabular-nums"
                >{formatMetric(embedderLatency.p50)} / {formatMetric(embedderLatency.p95)} ms</span
              >
              <span class="ml-1 text-s text-baba-text-faint">p50 / p95</span>
            {:else}
              <span class="text-baba-text-faint">—</span>
            {/if}
          </dd>
          <dt class="text-baba-text-muted">{t("system_field_face_models")}</dt>
          <dd>
            {#if info.face_models_present}
              <span class="text-emerald-400">● {t("system_field_face_present")}</span>
            {:else}
              <span class="text-baba-text-faint">{t("system_field_face_missing")}</span>
            {/if}
          </dd>
          <dt class="text-baba-text-muted">{t("system_field_plate_models")}</dt>
          <dd>
            {#if info.plate_reading === true}
              <span class="text-emerald-400">● {t("system_field_plate_on")}</span>
            {:else if info.plate_reading === false}
              <span class="text-baba-text-faint">{t("system_field_plate_off")}</span>
            {:else}
              <span class="text-baba-text-faint">—</span>
            {/if}
          </dd>
          <dt class="text-baba-text-muted">{t("system_field_auto_describe")}</dt>
          <dd>{info.auto_describe_enabled ? t("system_field_yes") : t("system_field_no")}</dd>
        </dl>
      </section>

      <section class="rounded-lg border border-baba-border bg-baba-panel">
        <header class="border-b border-baba-border px-4 py-2">
          <h3 class="text-m font-medium">{t("system_section_security")}</h3>
        </header>
        <dl class="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-2 px-4 py-3 text-m">
          <dt class="text-baba-text-muted">{t("system_field_cookie_secure")}</dt>
          <dd>
            {#if info.cookie_secure}
              <span class="text-emerald-400">● {t("system_field_yes")}</span>
            {:else}
              <span class="text-amber-400">{t("system_field_no")}</span>
            {/if}
          </dd>
          <dt class="text-baba-text-muted">{t("system_field_cors_origins")}</dt>
          <dd class="break-all font-mono text-s">{info.cors_origins.join(", ") || "—"}</dd>
          <dt class="text-baba-text-muted">{t("system_field_postgres_ssl")}</dt>
          <dd class="font-mono text-s">{info.postgres_sslmode}</dd>
        </dl>
        {#if !info.cookie_secure}
          <div class="border-t border-baba-border bg-baba-panel-2 px-4 py-2 text-s text-amber-400">
            ⚠ {t("system_warning_cookie_insecure")}
          </div>
        {/if}
        {#if corsIsDevDefault}
          <div class="border-t border-baba-border bg-baba-panel-2 px-4 py-2 text-s text-amber-400">
            ⚠ {t("system_warning_cors_default")}
          </div>
        {/if}
      </section>

      <section class="rounded-lg border border-baba-border bg-baba-panel">
        <header class="border-b border-baba-border px-4 py-2">
          <h3 class="text-m font-medium">{t("system_section_db")}</h3>
        </header>
        <dl class="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-2 px-4 py-3 text-m">
          <dt class="text-baba-text-muted">{t("dashboard_metric_cameras")}</dt>
          <dd>{stats.cameras_total} ({stats.cameras_enabled} {t("dashboard_metric_cameras_enabled")})</dd>
          <dt class="text-baba-text-muted">{t("dashboard_metric_events_total")}</dt>
          <dd>{formatNumber(stats.events_total, { maximumFractionDigits: 1 })}</dd>
          <dt class="text-baba-text-muted">tracks</dt>
          <dd>{formatNumber(stats.tracks_total, { maximumFractionDigits: 1 })}</dd>
          <dt class="text-baba-text-muted">{t("dashboard_metric_identities")}</dt>
          <dd>{stats.identities_total} ({stats.identities_labeled} {t("dashboard_metric_identities_labeled")})</dd>
          <dt class="text-baba-text-muted">{t("dashboard_metric_storage_segments")}</dt>
          <dd>{formatNumber(stats.recordings_total, { maximumFractionDigits: 1 })}</dd>
        </dl>
      </section>
    </div>
  {/if}
</div>
