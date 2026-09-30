<script lang="ts">
  import { onDestroy, onMount } from "svelte";
  import { api, type PurgeScope, type StorageUsage, type RecordingSettings } from "$lib/api";
  import { dialog, formatBytes, formatNumber, Button, Card, SaveButton } from "$lib/kit";
  import { t } from "$lib/i18n";
  import { dt } from "$lib/datetime.svelte";
  import { auth } from "$lib/auth.svelte";

  let usage = $state<StorageUsage | null>(null);
  let rs = $state<RecordingSettings | null>(null);
  let loading = $state(true);
  let error = $state<string | null>(null);

  // Danger-zone: purge all recordings.
  let purging = $state(false);

  // Recording-policy save state. (Per-camera retention is gone — the recorder
  // now applies one global policy from recording_settings.)
  let rsSaving = $state(false);
  let rsSaved = $state(false);
  let rsError = $state<string | null>(null);
  let rsStored = $state("");
  const rsDirty = $derived(rs !== null && JSON.stringify(policyOf(rs)) !== rsStored);

  function policyOf(r: RecordingSettings) {
    return {
      mode: r.mode,
      retention_days: r.retention_days,
      disk_high_water_pct: r.disk_high_water_pct,
      disk_low_water_pct: r.disk_low_water_pct,
      activity_buffer_minutes: r.activity_buffer_minutes,
    };
  }

  async function load() {
    loading = true;
    try {
      const [u, r] = await Promise.all([
        api.getStorageUsage(),
        api.getRecordingSettings(),
      ]);
      usage = u;
      rs = r;
      rsStored = JSON.stringify(policyOf(r));
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      loading = false;
    }
  }

  let gone = false;
  onMount(load);
  onDestroy(() => {
    gone = true;
  });

  async function saveRecording() {
    if (!rs) return;
    if (rs.disk_low_water_pct >= rs.disk_high_water_pct) {
      rsError = t("storage_lowwater_error");
      return;
    }
    rsSaving = true;
    rsError = null;
    try {
      rs = await api.putRecordingSettings(policyOf(rs));
      rsStored = JSON.stringify(policyOf(rs));
      rsSaved = true;
      setTimeout(() => (rsSaved = false), 1800);
    } catch (e) {
      rsError = e instanceof Error ? e.message : String(e);
    } finally {
      rsSaving = false;
    }
  }

  const segmentTotal = (u: StorageUsage | null): number =>
    u ? u.cameras.reduce((acc, c) => acc + c.segments, 0) : 0;

  async function purgeAll(scope: PurgeScope = "recordings") {
    const wide = scope === "record";
    const ok = await dialog.confirm({
      title: t(wide ? "storage_reset_confirm_title" : "storage_purge_confirm_title"),
      message: t(wide ? "storage_reset_confirm_msg" : "storage_purge_confirm_msg"),
      confirmLabel: t(wide ? "storage_reset_confirm_ok" : "storage_purge_confirm_ok"),
      danger: true,
    });
    if (!ok) return;
    purging = true;
    try {
      const res = await api.purgeAllRecordings(scope);
      // The API only SCHEDULES the wipe (the recorder does it off-request, so a
      // huge archive doesn't hit the Cloudflare timeout). Poll storage until
      // the indexed-segment count collapses, then declare done. Cap the poll so
      // a stuck recorder doesn't spin forever — the operator still got the
      // "scheduled" acknowledgement.
      const before = res.pending_rows;
      let remaining = before;
      for (let i = 0; i < 60; i++) {
        await new Promise((r) => setTimeout(r, 3000));
        // The loop runs for up to three minutes and `dialog` is the app-wide
        // singleton in the root layout, so neither is bound to this page:
        // navigating away left it polling and then popped a storage modal over
        // whatever the operator had moved to.
        if (gone) return;
        await load();
        remaining = segmentTotal(usage);
        if (remaining === 0 || remaining < before * 0.02) break;
      }
      if (gone) return;
      purging = false;
      const done = remaining === 0;
      await dialog.alert({
        title: done
          ? t(wide ? "storage_reset_done_title" : "storage_purge_done_title")
          : t("storage_purge_scheduled_title"),
        message: done
          ? t(wide ? "storage_reset_done_msg" : "storage_purge_done_msg")
          : t("storage_purge_scheduled_msg"),
      });
    } catch (e) {
      purging = false;
      await dialog.alert({
        title: t("storage_purge_fail_title"),
        message: e instanceof Error ? e.message : String(e),
      });
    }
  }

  const mediaUsedBytes = $derived(
    usage ? Math.max(0, usage.media_total_bytes - usage.media_free_bytes) : 0,
  );
  const mediaUsedPct = $derived(
    usage && usage.media_total_bytes > 0
      ? Math.round((mediaUsedBytes / usage.media_total_bytes) * 100)
      : 0,
  );

  // Per-camera contribution to the media volume as a fraction of the
  // total volume size. The remainder (`mediaUsedBytes - sum(cameras)`)
  // covers thumbnails, identity crops, and anything else that lives
  // under /media but isn't a tracked recording segment — we render
  // it as a neutral "other" slice so the bar still adds up.
  type Slice = { key: string; label: string; color: string; pct: number; bytes: number };
  const bytesAccountedFor = $derived(
    usage ? usage.cameras.reduce((acc, c) => acc + c.bytes, 0) : 0,
  );
  const otherBytes = $derived(Math.max(0, mediaUsedBytes - bytesAccountedFor));
  const usageColorWarn = $derived(mediaUsedPct >= 95 ? "#ef4444" : mediaUsedPct >= 80 ? "#f59e0b" : null);
  const slices = $derived<Slice[]>(
    usage && usage.media_total_bytes > 0
      ? [
          ...usage.cameras
            .filter((c) => c.bytes > 0)
            .map((c) => ({
              key: c.camera_id,
              label: c.name,
              // Tint cameras red/amber once the overall volume is in
              // the warning band so the operator's eye snaps to the
              // bar at a glance — exact per-camera colors are still
              // visible in the legend underneath.
              color: usageColorWarn ?? c.color,
              pct: (c.bytes / (usage?.media_total_bytes || 1)) * 100,
              bytes: c.bytes,
            })),
          ...(otherBytes > 0
            ? [{
                key: "_other",
                label: t("storage_legend_other"),
                color: "#475569",  // slate-600, deliberately muted
                pct: (otherBytes / usage.media_total_bytes) * 100,
                bytes: otherBytes,
              }]
            : []),
        ]
      : [],
  );
</script>

<div class="space-y-6">

  {#if loading}
    <p class="text-m text-baba-text-muted">{t("storage_loading")}</p>
  {:else if error}
    <p class="text-m text-red-400">{t("cameras_error_prefix")}: {error}</p>
  {:else if usage}
    <!-- Media volume summary -->
    <Card title={t("storage_media_path")}>
      {#snippet actions()}
        {#if usage}
          <span class="text-right text-s text-baba-text-muted">
            {formatBytes(mediaUsedBytes)} {t("storage_media_used")} ·
            {formatBytes(usage.media_free_bytes)} {t("storage_media_free")} ·
            {formatBytes(usage.media_total_bytes)} {t("storage_media_total")}
          </span>
        {/if}
      {/snippet}
      <p class="font-mono text-m">{usage.media_path}</p>
      <div class="mt-3 flex h-2 w-full overflow-hidden rounded-full bg-baba-panel-2">
        {#each slices as s (s.key)}
          <div
            class="h-full transition-all"
            style="width: {s.pct}%; background-color: {s.color};"
            title="{s.label} · {formatBytes(s.bytes)} ({formatNumber(s.pct, { maximumFractionDigits: 1 })}%)"
          ></div>
        {/each}
      </div>
      {#if slices.length > 0}
        <ul class="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-s text-baba-text-muted">
          {#each slices as s (s.key)}
            <li class="flex items-center gap-1.5">
              <span
                class="inline-block h-2.5 w-2.5 rounded-sm ring-1 ring-baba-border"
                style="background-color: {s.color}"
                aria-hidden="true"
              ></span>
              <span>{s.label}</span>
              <span class="tabular-nums text-baba-text-faint">{formatBytes(s.bytes)}</span>
            </li>
          {/each}
        </ul>
      {/if}
    </Card>

    <!-- Fast tier. Only rendered when the small-file dirs actually sit on
         their own volume (the API reports null otherwise), so a single-disk
         install shows one bar and nobody wonders what the empty second one
         means. No per-camera breakdown here: this tier holds crops, the clip
         cache, thumbnails and identity/scene photos — not per-camera footage. -->
    {#if rs?.disk_fast}
      <Card title={t("storage_fast_title")}>
        {#snippet actions()}
          {#if rs?.disk_fast}
            <span class="text-right text-s text-baba-text-muted">
              {formatBytes(rs.disk_fast.used_bytes)} {t("storage_media_used")} ·
              {formatBytes(rs.disk_fast.free_bytes)} {t("storage_media_free")} ·
              {formatBytes(rs.disk_fast.total_bytes)} {t("storage_media_total")}
            </span>
          {/if}
        {/snippet}
        <p class="text-m text-baba-text-muted">{t("storage_fast_desc")}</p>
        <div class="mt-3 flex h-2 w-full overflow-hidden rounded-full bg-baba-panel-2">
          <div
            class="h-full transition-all"
            style="width: {rs.disk_fast.used_pct}%; background-color: #22d3ee;"
            title="{formatBytes(rs.disk_fast.used_bytes)} ({rs.disk_fast.used_pct}%)"
          ></div>
        </div>
        <div class="mt-2 text-s text-baba-text-faint tabular-nums">
          {rs.disk_fast.used_pct}% {t("storage_fast_used_of")}
        </div>
      </Card>
    {/if}

    <!-- Recording policy (mode / retention / disk safety net) -->
    {#if rs}
      <Card title={t("settings_recording_title")}>
        {#if rsError}<p class="text-m text-red-400">{rsError}</p>{/if}

        <div class="space-y-2">
          <div class="text-s uppercase tracking-wide text-baba-text-faint">{t("rec_mode")}</div>
          {#each [["continuous", "rec_mode_continuous", "rec_mode_continuous_desc"], ["activity", "rec_mode_activity", "rec_mode_activity_desc"]] as [val, label, desc] (val)}
            <button
              type="button"
              onclick={() => (rs!.mode = val as RecordingSettings["mode"])}
              class="flex w-full items-start gap-3 rounded-lg border p-3 text-left {rs.mode === val ? 'border-baba-accent bg-baba-panel-2' : 'border-baba-border hover:bg-baba-panel-2'}"
            >
              <span class="mt-0.5 grid h-4 w-4 shrink-0 place-items-center rounded-full border {rs.mode === val ? 'border-baba-accent' : 'border-baba-text-faint'}">
                {#if rs.mode === val}<span class="h-2 w-2 rounded-full bg-baba-accent"></span>{/if}
              </span>
              <span class="min-w-0">
                <span class="block text-m">{t(label as never)}</span>
                <span class="block text-s text-baba-text-faint">{t(desc as never)}</span>
              </span>
            </button>
          {/each}
        </div>

        <div class="flex items-center justify-between gap-4">
          <div class="min-w-0">
            <div class="text-m">{t("rec_retention")}</div>
            <div class="text-s text-baba-text-faint">{t("rec_retention_desc")}</div>
          </div>
          <input type="number" min="1" max="365" bind:value={rs.retention_days}
            class="w-24 rounded border border-baba-border bg-baba-bg px-2 py-1 text-right text-m" />
        </div>

        {#if rs.mode === "activity"}
          <div class="flex items-center justify-between gap-4">
            <div class="min-w-0">
              <div class="text-m">{t("rec_activity_buffer")}</div>
              <div class="text-s text-baba-text-faint">{t("rec_activity_buffer_desc")}</div>
            </div>
            <input type="number" min="0" max="60" bind:value={rs.activity_buffer_minutes}
              class="w-24 rounded border border-baba-border bg-baba-bg px-2 py-1 text-right text-m" />
          </div>
        {/if}

        <div class="border-t border-baba-border pt-3">
          <div class="text-s text-baba-text-faint">{t("rec_disk_desc")}</div>
          <div class="mt-2 flex items-center justify-between gap-4">
            <div class="text-m">{t("rec_disk_high")}</div>
            <input type="number" min="50" max="99" bind:value={rs.disk_high_water_pct}
              class="w-24 rounded border border-baba-border bg-baba-bg px-2 py-1 text-right text-m" />
          </div>
          <div class="mt-2 flex items-center justify-between gap-4">
            <div class="text-m">{t("rec_disk_low")}</div>
            <input type="number" min="40" max="98" bind:value={rs.disk_low_water_pct}
              class="w-24 rounded border border-baba-border bg-baba-bg px-2 py-1 text-right text-m" />
          </div>
        </div>

        <div class="flex items-center gap-3">
          <SaveButton dirty={rsDirty} saving={rsSaving} onclick={saveRecording} />
          {#if rsSaved}<span class="text-s text-emerald-400">{t("rec_saved")}</span>{/if}
        </div>
      </Card>
    {/if}

    <!-- Per-camera breakdown (sizes only — retention is global now) -->
    <section class="rounded-lg border border-baba-border bg-baba-panel">
      <header class="border-b border-baba-border px-4 py-2">
        <h3 class="text-m font-medium">{t("storage_section_per_camera")}</h3>
      </header>
      {#if usage.cameras.length === 0}
        <p class="px-4 py-6 text-m text-baba-text-faint">
          {t("storage_empty_cameras")}
        </p>
      {:else}
        <div class="overflow-x-auto">
          <table class="w-full text-m">
            <thead class="bg-baba-panel-2 text-s uppercase tracking-wide text-baba-text-faint">
              <tr>
                <th class="px-4 py-2 text-left">{t("storage_col_camera")}</th>
                <th class="px-4 py-2 text-right">{t("storage_col_segments")}</th>
                <th class="px-4 py-2 text-right">{t("storage_col_size")}</th>
                <th class="px-4 py-2 text-left">{t("storage_col_oldest")}</th>
                <th class="px-4 py-2 text-left">{t("storage_col_newest")}</th>
              </tr>
            </thead>
            <tbody class="divide-y divide-baba-border">
              {#each usage.cameras as cam (cam.camera_id)}
                <tr>
                  <td class="px-4 py-2">
                    <span class="inline-flex items-center gap-2">
                      <span
                        class="inline-block h-2.5 w-2.5 rounded-sm ring-1 ring-baba-border"
                        style="background-color: {cam.color}"
                        aria-hidden="true"
                      ></span>
                      <a href={`/settings/cameras/${cam.camera_id}`} class="hover:underline">
                        {cam.name}
                      </a>
                      <span class="text-s text-baba-text-faint">{cam.slug}</span>
                    </span>
                  </td>
                  <td class="px-4 py-2 text-right tabular-nums">
                    {formatNumber(cam.segments, { maximumFractionDigits: 1 })}
                  </td>
                  <td class="px-4 py-2 text-right tabular-nums">{formatBytes(cam.bytes)}</td>
                  <td class="px-4 py-2 text-s text-baba-text-muted">
                    {cam.oldest_at ? dt.short(cam.oldest_at) : t("storage_no_data")}
                  </td>
                  <td class="px-4 py-2 text-s text-baba-text-muted">
                    {cam.newest_at ? dt.short(cam.newest_at) : t("storage_no_data")}
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    </section>

    <!-- Danger zone: wipe every recording (admin only) -->
    {#if auth.user?.role === "admin"}
      <section class="rounded-lg border border-red-700/40 bg-red-500/5 p-4">
        <h3 class="text-m font-medium text-red-400">{t("storage_danger_title")}</h3>
        <p class="mt-1 text-s text-baba-text-muted">{t("storage_danger_desc")}</p>
        <div class="mt-3"><Button tone="danger" onclick={() => purgeAll("recordings")} disabled={purging}>{purging ? t("storage_purge_running") : t("storage_purge_btn")}</Button></div>

        <p class="mt-5 border-t border-red-700/40 pt-4 text-s text-baba-text-muted">
          {t("storage_reset_desc")}
        </p>
        <div class="mt-3"><Button tone="danger" onclick={() => purgeAll("record")} disabled={purging}>{purging ? t("storage_purge_running") : t("storage_reset_btn")}</Button></div>
      </section>
    {/if}
  {/if}
</div>
