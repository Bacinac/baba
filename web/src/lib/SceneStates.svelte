<script lang="ts">
  import { dialog, formatNumber, Button, Card, Tag } from "$lib/kit";
  import { t } from "$lib/i18n";
  // Scene-state regions editor: draw a fixed polygon over the live frame,
  // label its states (e.g. open/closed), capture a few reference frames per
  // state, and the state-evaluator classifies the region's persistent state on
  // a slow cadence. Distinct from ZoneEditor (which tests transient tracks) —
  // this is about the standing state of scene structure (a gate, a door).
  import { onDestroy, onMount } from "svelte";
  import {
    api,
    thumbnailUrl,
    type SceneEvalResult,
    type SceneRegion,
    type ScenePrototype,
  } from "$lib/api";
  import { dt } from "$lib/datetime.svelte";
  import LiveStream from "$lib/LiveStream.svelte";

  let {
    cameraId,
    cameraSlug,
    hasSubstream = false,
  }: { cameraId: string; cameraSlug: string; hasSubstream?: boolean } = $props();

  let regions = $state<SceneRegion[]>([]);
  let loading = $state(true);
  let loadError = $state<string | null>(null);

  // Polygon drawing state.
  let drawing = $state(false);
  let drawPts = $state<[number, number][]>([]);
  let svgEl = $state<SVGSVGElement | undefined>();

  // Per-region "capture from this recorded moment instead of live" wall-clock
  // (datetime-local, so browser-local and naive). Empty = capture live.
  let captureAt = $state<Record<string, string>>({});

  // New-region form, shown after a polygon is finished.
  let formOpen = $state(false);
  let formName = $state("");
  let formPlace = $state("");
  let formStates = $state("otvoreno, zatvoreno");
  let creating = $state(false);
  let createError = $state<string | null>(null);

  // Per-region transient UI: which action is in flight, and last test result.
  let busy = $state<Record<string, string>>({});
  let testResults = $state<Record<string, SceneEvalResult>>({});

  let pollTimer: ReturnType<typeof setInterval> | null = null;

  async function load(statusOnly = false) {
    try {
      // unknown_margin is a float32 in the DB, so it widens to e.g.
      // 0.4000000059604645 — round it back for a clean number input.
      const fresh = (await api.listSceneRegions(cameraId)).map((r) => ({
        ...r,
        unknown_margin: Math.round(r.unknown_margin * 1000) / 1000,
      }));
      if (statusOnly && regions.length) {
        // Background poll: refresh only the server-owned status (current_state,
        // last_eval_at, …) and keep the operator-editable config fields the
        // settings inputs are bound to. A wholesale replace here silently
        // reset any value the operator was mid-typing every 10 s.
        regions = fresh.map((fr) => {
          const cur = regions.find((r) => r.id === fr.id);
          return cur ? { ...cur, status: fr.status } : fr;
        });
      } else {
        regions = fresh;
      }
      loadError = null;
    } catch (e) {
      loadError = (e as Error).message;
    } finally {
      loading = false;
    }
  }

  onMount(() => {
    load();
    // Refresh status badges as the evaluator commits transitions — status only,
    // so it never clobbers in-progress edits to the region settings.
    pollTimer = setInterval(() => load(true), 10000);
  });
  onDestroy(() => {
    if (pollTimer) clearInterval(pollTimer);
  });

  function ptsAttr(poly: [number, number][]): string {
    return poly.map(([x, y]) => `${x * 100},${y * 100}`).join(" ");
  }

  function startDraw() {
    drawing = true;
    drawPts = [];
    formOpen = false;
    createError = null;
  }
  function cancelDraw() {
    drawing = false;
    drawPts = [];
    formOpen = false;
  }
  function onSvgClick(e: MouseEvent) {
    if (!drawing || !svgEl) return;
    const rect = svgEl.getBoundingClientRect();
    const x = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
    const y = Math.min(1, Math.max(0, (e.clientY - rect.top) / rect.height));
    drawPts = [...drawPts, [x, y]];
  }
  function finishDraw() {
    if (drawPts.length < 3) return;
    drawing = false;
    formOpen = true;
    formName = "";
    formPlace = "";
  }
  function onDblClick() {
    // The dblclick is preceded by two single clicks that each added a point at
    // ~the same spot — drop the duplicate, then close the polygon.
    if (drawPts.length >= 4) drawPts = drawPts.slice(0, -1);
    finishDraw();
  }
  function onKey(e: KeyboardEvent) {
    if (!drawing) return;
    if (e.key === "Enter") finishDraw();
    else if (e.key === "Escape") cancelDraw();
    else if (e.key === "Backspace") drawPts = drawPts.slice(0, -1);
  }

  async function createRegion() {
    const states = formStates
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    creating = true;
    createError = null;
    try {
      await api.createSceneRegion(cameraId, {
        name: formName.trim() || t("scene_new_region"),
        place: formPlace.trim() || null,
        polygon: drawPts,
        states,
      });
      cancelDraw();
      await load();
    } catch (e) {
      createError = (e as Error).message;
    } finally {
      creating = false;
    }
  }

  function setBusy(id: string, label: string | null) {
    if (label === null) {
      const { [id]: _drop, ...rest } = busy;
      busy = rest;
    } else {
      busy = { ...busy, [id]: label };
    }
  }

  // The evaluator answers with stable slugs. Map the ones an operator can act
  // on; anything unexpected still shows verbatim rather than vanishing.
  function captureErrorText(reason: string): string {
    switch (reason) {
      case "no_recording":
        return t("scene_capture_err_no_recording");
      case "recording_file_missing":
        return t("scene_capture_err_recording_file_missing");
      case "no_frame_at_timestamp":
        return t("scene_capture_err_no_frame_at_timestamp");
      case "decode_failed":
        return t("scene_capture_err_decode_failed");
      case "decode_timeout":
        return t("scene_capture_err_decode_timeout");
      case "no_frame":
        return t("scene_capture_err_no_frame");
      case "bad_region":
        return t("scene_capture_err_bad_region");
      default:
        return reason;
    }
  }

  // Latest selectable moment, in the format datetime-local wants. Recomputed
  // per render so the max never lags behind the clock.
  function nowLocalValue(): string {
    const d = new Date();
    d.setMinutes(d.getMinutes() - d.getTimezoneOffset());
    return d.toISOString().slice(0, 16);
  }

  async function capture(r: SceneRegion, label: string) {
    setBusy(r.id, `cap:${label}`);
    try {
      const local = captureAt[r.id];
      // datetime-local hands back a naive wall-clock string; resolving it
      // through Date applies the browser's timezone, so the server gets an
      // unambiguous instant rather than our guess at the operator's offset.
      let at: string | undefined;
      if (local) {
        const parsed = new Date(local);
        if (Number.isNaN(parsed.getTime())) {
          await dialog.alert({
            title: t("scene_capture"),
            message: t("scene_capture_at_invalid"),
          });
          return;
        }
        at = parsed.toISOString();
      }
      const res = await api.captureScenePrototype(r.id, label, at);
      if (res.error) {
        await dialog.alert({
          title: t("scene_capture"),
          message: captureErrorText(res.error),
        });
      }
      await load();
    } catch (e) {
      await dialog.alert({ title: t("scene_capture"), message: (e as Error).message });
    } finally {
      setBusy(r.id, null);
    }
  }

  async function test(r: SceneRegion) {
    setBusy(r.id, "test");
    try {
      testResults = { ...testResults, [r.id]: await api.evaluateSceneRegion(r.id) };
    } catch (e) {
      testResults = {
        ...testResults,
        [r.id]: {
          state: null,
          raw_label: null,
          distance: null,
          per_state: {},
          error: (e as Error).message,
        },
      };
    } finally {
      setBusy(r.id, null);
    }
  }

  async function setPlace(r: SceneRegion, raw: string) {
    const place = raw.trim() || null;
    if (place === (r.place ?? null)) return;
    try {
      await api.patchSceneRegion(r.id, { place });
      await load();
    } catch (e) {
      await dialog.alert({ title: t("scene_region_name"), message: (e as Error).message });
    }
  }

  async function renameRegion(r: SceneRegion) {
    const name = r.name.trim();
    if (!name) { await load(); return; } // reject empty — reload the stored name
    setBusy(r.id, "save");
    try {
      await api.patchSceneRegion(r.id, { name });
    } catch (e) {
      await dialog.alert({ title: t("scene_region_name"), message: (e as Error).message });
      await load();
    } finally {
      setBusy(r.id, null);
    }
  }

  async function saveSettings(r: SceneRegion) {
    setBusy(r.id, "save");
    try {
      await api.patchSceneRegion(r.id, {
        sample_interval_s: r.sample_interval_s,
        hysteresis_n: r.hysteresis_n,
        unknown_margin: r.unknown_margin,
        enabled: r.enabled,
      });
      await load();
    } catch (e) {
      await dialog.alert({ title: t("scene_save"), message: (e as Error).message });
    } finally {
      setBusy(r.id, null);
    }
  }

  async function removeRegion(r: SceneRegion) {
    const ok = await dialog.confirm({
      title: t("scene_delete_region"),
      message: t("scene_delete_region_confirm"),
      danger: true,
    });
    if (!ok) return;
    try {
      await api.deleteSceneRegion(r.id);
      await load();
    } catch (e) {
      await dialog.alert({ title: t("scene_delete_region"), message: (e as Error).message });
    }
  }

  async function removePrototype(p: ScenePrototype) {
    const ok = await dialog.confirm({
      title: t("scene_references"),
      message: t("scene_delete_prototype_confirm"),
      danger: true,
    });
    if (!ok) return;
    try {
      await api.deleteScenePrototype(p.id);
      await load();
    } catch (e) {
      await dialog.alert({ title: t("scene_references"), message: (e as Error).message });
    }
  }

  function protosFor(r: SceneRegion, label: string): ScenePrototype[] {
    return r.prototypes.filter((p) => p.state_label === label);
  }
</script>

<svelte:window onkeydown={onKey} />

<section class="space-y-4">
  <header class="flex items-start justify-between gap-4">
    <div>
      <h3 class="text-xl font-semibold">{t("scene_states_title")}</h3>
      <p class="mt-1 text-m text-baba-text-faint">{t("scene_states_desc")}</p>
    </div>
    {#if !drawing && !formOpen}
      <div class="shrink-0"><Button tone="accent" onclick={startDraw}>+ {t("scene_new_region")}</Button></div>
    {/if}
  </header>

  <!-- Drawing toolbar: sticky so it stays reachable while clicking points on a
       tall (square / fisheye) live frame that scrolls past the viewport. -->
  {#if drawing}
    <div
      class="sticky top-2 z-20 flex flex-wrap items-center gap-3 rounded border border-baba-accent/40 bg-baba-panel/95 px-3 py-2 text-m shadow-lg backdrop-blur"
    >
      <span class="text-baba-text-faint">{t("scene_draw_hint")}</span>
      <Button tone="primary" size="small" onclick={finishDraw} disabled={drawPts.length < 3}>OK ({drawPts.length})</Button>
      <Button size="small" onclick={cancelDraw}>{t("scene_cancel")}</Button>
    </div>
  {/if}

  <!-- Live frame + polygon overlay. Height-capped (max-h) + an inline-block
       wrapper that shrinks to the aspect-preserved video, so the SVG overlay
       tracks the video exactly (no letterbox skew) AND the create form + region
       cards below stay on-screen for tall square/fisheye cameras. -->
  <div class="flex justify-center">
  <div class="relative inline-block overflow-hidden rounded border border-baba-border">
    <LiveStream slug={cameraSlug} {hasSubstream} class="block h-auto max-h-[60vh] w-auto max-w-full" />
    <svg
      bind:this={svgEl}
      onclick={onSvgClick}
      ondblclick={onDblClick}
      role="presentation"
      viewBox="0 0 100 100"
      preserveAspectRatio="none"
      class="absolute inset-0 h-full w-full {drawing ? 'cursor-crosshair' : 'pointer-events-none'}"
    >
      {#each regions as r (r.id)}
        <polygon
          points={ptsAttr(r.polygon)}
          fill={r.color}
          fill-opacity={r.enabled ? 0.18 : 0.06}
          stroke={r.color}
          stroke-width="0.4"
        />
      {/each}
      {#if drawing && drawPts.length}
        <!-- Closed + filled once there are >= 3 points so the enclosed area is
             obvious; a dashed open polyline while still placing the first few. -->
        {#if drawPts.length >= 3}
          <polygon
            points={ptsAttr(drawPts)}
            fill="#22d3ee"
            fill-opacity="0.2"
            stroke="#22d3ee"
            stroke-width="0.4"
            stroke-dasharray="1 1"
          />
        {:else}
          <polyline
            points={ptsAttr(drawPts)}
            fill="none"
            stroke="#22d3ee"
            stroke-width="0.4"
            stroke-dasharray="1 1"
          />
        {/if}
        {#each drawPts as p}
          <circle cx={p[0] * 100} cy={p[1] * 100} r="0.8" fill="#22d3ee" />
        {/each}
      {/if}
    </svg>
  </div>
  </div>

  {#if formOpen}
    <Card>
      <div>
        <label class="block text-s text-baba-text-faint" for="scene-name">{t("scene_region_name")}</label>
        <input
          id="scene-name"
          bind:value={formName}
          placeholder={t("scene_region_name")}
          class="mt-1 w-full rounded border border-baba-border bg-baba-bg px-2 py-1.5 text-m"
        />
      </div>
      <div>
        <!-- The physical thing this region watches. Two cameras that can see one
             parking space give it the same place, and everything downstream can
             then treat them as one spot instead of guessing from time overlap. -->
        <label class="block text-s text-baba-text-faint" for="scene-place">{t("scene_region_place")}</label>
        <input
          id="scene-place"
          bind:value={formPlace}
          class="mt-1 w-full rounded border border-baba-border bg-baba-bg px-2 py-1.5 text-m"
        />
      </div>
      <div>
        <label class="block text-s text-baba-text-faint" for="scene-states">{t("scene_states_labels")}</label>
        <input
          id="scene-states"
          bind:value={formStates}
          placeholder={t("scene_states_labels_hint")}
          class="mt-1 w-full rounded border border-baba-border bg-baba-bg px-2 py-1.5 text-m"
        />
      </div>
      {#if createError}<p class="text-m text-red-400">{createError}</p>{/if}
      <div class="flex gap-2">
        <Button tone="primary" onclick={createRegion} disabled={creating}>{t("scene_create")}</Button>
        <Button onclick={cancelDraw}>{t("scene_cancel")}</Button>
      </div>
    </Card>
  {/if}

  {#if loading}
    <p class="text-m text-baba-text-faint">…</p>
  {:else if loadError}
    <p class="text-m text-red-400">{loadError}</p>
  {:else if regions.length === 0}
    <p class="text-m text-baba-text-faint">{t("scene_no_regions")}</p>
  {:else}
    <div class="space-y-4">
      {#each regions as r (r.id)}
        {@const tr = testResults[r.id]}
        <Card>
          <div class="flex flex-wrap items-center justify-between gap-2">
            <div class="flex items-center gap-2">
              <span class="inline-block h-3 w-3 rounded-full" style="background:{r.color}"></span>
              <!-- Inline rename: the region name is editable in place; commits
                   on blur / Enter (the API + backend already accept a name
                   patch, the UI just never exposed it). -->
              <input
                type="text"
                bind:value={r.name}
                onblur={() => renameRegion(r)}
                onkeydown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }}
                aria-label={t("scene_region_name")}
                class="min-w-0 rounded border border-transparent bg-transparent px-1 py-0.5 font-medium hover:border-baba-border focus:border-baba-accent focus:outline-none"
              />
            </div>
            <div class="flex items-center gap-2 text-s">
              <span class="text-baba-text-faint">{t("scene_region_place")}:</span>
              <input
                type="text"
                value={r.place ?? ""}
                onblur={(e) => setPlace(r, (e.target as HTMLInputElement).value)}
                onkeydown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }}
                aria-label={t("scene_region_place")}
                class="min-w-0 rounded border border-transparent bg-transparent px-1 py-0.5 font-mono
                       hover:border-baba-border focus:border-baba-accent focus:outline-none"
              />
            </div>
            <div class="flex items-center gap-3 text-m">
              <span class="text-baba-text-faint">{t("scene_current_state")}:</span>
              <span class="font-mono"><Tag>{r.status?.current_state ?? "—"}</Tag></span>
              {#if r.status?.current_state_since}
                <span class="text-s text-baba-text-faint">{dt.hms(r.status.current_state_since)}</span>
              {/if}
            </div>
          </div>

          <!-- States + reference capture -->
          <div class="mt-3 space-y-3">
            {#if r.states.length < 2}
              <p class="text-s text-amber-400">{t("scene_need_two_states")}</p>
            {/if}
            {#each r.states as label}
              {@const protos = protosFor(r, label)}
              {@const measured = label === "blinded"}
              <div class="grid grid-cols-[5.5rem_1fr_auto] items-center gap-3">
                <span class="font-mono text-m">{label}</span>
                {#if measured}
                  <span class="text-s text-baba-text-faint">{t("scene_measured_state")}</span>
                  <span></span>
                {:else}
                <div class="flex flex-wrap gap-1">
                  {#each protos as p (p.id)}
                    <div class="group relative">
                      {#if p.crop_path}
                        <img
                          src={thumbnailUrl(p.crop_path)}
                          alt={label}
                          class="h-16 w-auto rounded object-contain"
                        />
                      {/if}
                      <button
                        onclick={() => removePrototype(p)}
                        class="absolute -right-1 -top-1 hidden h-4 w-4 items-center justify-center rounded-full bg-red-600 text-2xs text-white group-hover:flex"
                        aria-label={t("dialog_delete")}
                      >×</button>
                    </div>
                  {/each}
                  {#if protos.length === 0}
                    <span class="text-s text-baba-text-faint">{t("scene_no_prototypes")}</span>
                  {/if}
                </div>
                <div class="shrink-0"><Button size="small" onclick={() => capture(r, label)} disabled={busy[r.id] === `cap:${label}`}>{busy[r.id] === `cap:${label}` ? t("scene_capturing") : t("scene_capture")}</Button></div>
                {/if}
              </div>
            {/each}
            <div class="flex flex-wrap items-center gap-2">
              <label class="text-s text-baba-text-faint" for={`scene-at-${r.id}`}>
                {t("scene_capture_at")}
              </label>
              <input
                id={`scene-at-${r.id}`}
                type="datetime-local"
                max={nowLocalValue()}
                bind:value={
                  () => captureAt[r.id] ?? "",
                  (v) => (captureAt = { ...captureAt, [r.id]: v })
                }
                class="rounded border border-baba-border bg-baba-bg px-2 py-1 text-s text-baba-text"
              />
              {#if captureAt[r.id]}
                <Button size="small" onclick={() => (captureAt = { ...captureAt, [r.id]: "" })}>{t("scene_capture_at_clear")}</Button>
              {/if}
            </div>
            <p class="text-s text-baba-text-faint">{t("scene_capture_at_hint")}</p>
            <p class="text-s text-baba-text-faint">{t("scene_lighting_hint")}</p>
          </div>

          <!-- Test + diagnostics -->
          <div class="mt-3 flex flex-wrap items-center gap-3">
            <Button size="small" onclick={() => test(r)} disabled={busy[r.id] === "test"}>{busy[r.id] === "test" ? t("scene_testing") : t("scene_test")}</Button>
            {#if tr}
              {#if tr.error}
                <span class="text-m text-red-400">{tr.error}</span>
              {:else}
                <span class="text-m">
                  {t("scene_raw_label")}:
                  <span class="font-mono">{tr.raw_label ?? t("scene_unknown")}</span>
                  {#if tr.distance !== null}
                    · {t("scene_distance")} {formatNumber(tr.distance, { minimumFractionDigits: 3, maximumFractionDigits: 3 })}
                  {/if}
                </span>
                {#if Object.keys(tr.per_state).length}
                  <span class="flex flex-wrap items-center gap-1.5 text-s text-baba-text-faint">
                    {#each Object.entries(tr.per_state) as [k, v]}
                      <span class="font-mono"><Tag tone="quiet">{k} {formatNumber(v, { minimumFractionDigits: 3, maximumFractionDigits: 3 })}</Tag></span>
                    {/each}
                  </span>
                {/if}
              {/if}
            {/if}
          </div>

          <!-- Settings. Each cell is a full-height flex column with the input
               pinned to the bottom (mt-auto), so the inputs line up even when
               some labels wrap to two lines and others don't. -->
          <div class="mt-4 grid grid-cols-2 items-end gap-3 sm:grid-cols-4">
            <label class="flex h-full flex-col text-s text-baba-text-faint">
              <span>{t("scene_sample_interval")}</span>
              <input
                type="number"
                min="1"
                bind:value={r.sample_interval_s}
                class="mt-auto w-full rounded border border-baba-border bg-baba-bg px-2 py-1 text-m text-baba-text"
              />
            </label>
            <label class="flex h-full flex-col text-s text-baba-text-faint">
              <span>{t("scene_hysteresis")}</span>
              <input
                type="number"
                min="1"
                bind:value={r.hysteresis_n}
                class="mt-auto w-full rounded border border-baba-border bg-baba-bg px-2 py-1 text-m text-baba-text"
              />
            </label>
            <label class="flex h-full flex-col text-s text-baba-text-faint">
              <span>{t("scene_unknown_margin")}</span>
              <input
                type="number"
                min="0"
                max="2"
                step="0.05"
                bind:value={r.unknown_margin}
                class="mt-auto w-full rounded border border-baba-border bg-baba-bg px-2 py-1 text-m text-baba-text"
              />
            </label>
            <label class="flex items-center gap-2 pb-1.5 text-m">
              <input type="checkbox" bind:checked={r.enabled} />
              {t("scene_enabled")}
            </label>
          </div>
          <p class="mt-2 text-s text-baba-text-faint">
            {t("scene_reaction_hint")} ≈ <span class="font-mono text-baba-text">{r.sample_interval_s * r.hysteresis_n}s</span>
          </p>

          <div class="mt-3 flex items-center justify-between">
            <Button tone="accent" onclick={() => saveSettings(r)} disabled={busy[r.id] === "save"}>{busy[r.id] === "save" ? "…" : t("scene_save")}</Button>
            <Button tone="danger" onclick={() => removeRegion(r)}>{t("scene_delete_region")}</Button>
          </div>
        </Card>
      {/each}
    </div>
  {/if}
</section>
