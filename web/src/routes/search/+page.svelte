<script lang="ts">
  import { byLabel } from "$lib/order";
  import { formatNumber, ApiError, Button, Card, Tag } from "$lib/kit";
  import { t } from "$lib/i18n";
  import { onMount, onDestroy } from "svelte";
  import {
    api,
    thumbnailUrl,
    type Camera,
    type SearchHit,
    type SearchResult,
  } from "$lib/api";
  import { CLASS_GROUPS } from "$lib/classGroups";
  import { dt } from "$lib/datetime.svelte";

  // --- query state ---

  let cameras = $state<Camera[]>([]);
  let queryFile = $state<File | null>(null);
  let queryPreview = $state<string>("");
  let cameraFilter = $state<string>("");
  let classGroupFilter = $state<string>("");
  let threshold = $state<number>(0.55);
  let limit = $state<number>(24);
  let loading = $state<boolean>(false);
  let error = $state<string | null>(null);
  let result = $state<SearchResult | null>(null);
  let dragActive = $state<boolean>(false);
  let fileInput: HTMLInputElement | null = $state(null);

  const MAX_FILE_BYTES = 8 * 1024 * 1024;

  // Same class grouping the events page uses — keeps the UI coherent
  // and the backend stays class-id-agnostic (the filter is collapsed
  // to a single class_id at send time by picking the first of the
  // group; broader filtering is post-hoc in the UI).
  // CLASS_GROUPS (with COCO ids) shared with events — see $lib/classGroups.

  onMount(async () => {
    try {
      cameras = await api.listCameras();
    } catch (e) {
      // Cameras list is convenience-only — search still works without
      // the per-camera filter.
      console.warn("search: cameras list failed", e);
    }
  });

  function pickFile(f: File | null) {
    error = null;
    result = null;
    if (queryPreview) URL.revokeObjectURL(queryPreview);
    queryPreview = "";
    queryFile = null;
    if (!f) return;
    if (f.size > MAX_FILE_BYTES) {
      error = t("search_error_too_big");
      return;
    }
    queryFile = f;
    queryPreview = URL.createObjectURL(f);
    // Auto-search on pick. The user can refine with filters and
    // re-search via the form; first-shot UX is "drop image, see results".
    runSearch();
  }

  // Race guard, as every other loader in this app has. The three filter
  // controls call this on change and none of them is disabled while it runs,
  // so two searches can be in flight and the slower one can land last —
  // leaving results that belong to a filter the operator has already moved off.
  let searchSeq = 0;

  async function runSearch() {
    if (!queryFile) return;
    const mySeq = ++searchSeq;
    loading = true;
    error = null;
    try {
      const group = CLASS_GROUPS.find((g) => g.id === classGroupFilter);
      // The endpoint takes a single class_id; for vehicle/animal groups
      // we don't filter on the server (kNN ordering picks the right
      // hits anyway) and rely on class-name filtering client-side if
      // needed later. Person and bicycle are single-class so the
      // server filter is exact.
      const class_id =
        group && group.ids.length === 1 ? group.ids[0] : undefined;
      const r = await api.searchVisual(queryFile, {
        limit,
        class_id,
        camera_id: cameraFilter || undefined,
        max_distance: threshold,
      });
      if (mySeq !== searchSeq) return;
      result = r;
    } catch (e) {
      if (mySeq !== searchSeq) return;
      // Backend returns 503 with a message when embedder isn't loaded.
      error = e instanceof ApiError && e.status === 503
        ? t("search_error_disabled")
        : (e as Error).message;
    } finally {
      if (mySeq === searchSeq) loading = false;
    }
  }

  function clearAll() {
    if (queryPreview) URL.revokeObjectURL(queryPreview);
    queryFile = null;
    queryPreview = "";
    result = null;
    error = null;
    if (fileInput) fileInput.value = "";
  }

  // Revoke a lingering preview object-URL if the page is left with one set —
  // clearAll/re-select handle the in-page cases, this covers navigation away.
  onDestroy(() => {
    if (queryPreview) URL.revokeObjectURL(queryPreview);
  });

  function onDrop(e: DragEvent) {
    e.preventDefault();
    dragActive = false;
    const f = e.dataTransfer?.files?.[0];
    if (f) pickFile(f);
  }

  function onPaste(e: ClipboardEvent) {
    const items = e.clipboardData?.items ?? [];
    for (const it of items) {
      if (it.kind === "file") {
        const f = it.getAsFile();
        if (f) {
          pickFile(f);
          return;
        }
      }
    }
  }

  // Client-side filter for vehicle/animal groups (server returns the
  // raw top-K hits and we narrow to the user-selected group).
  const visibleHits = $derived.by((): SearchHit[] => {
    const hits = result?.hits ?? [];
    const group = CLASS_GROUPS.find((g) => g.id === classGroupFilter);
    if (!group || group.ids.length === 1) return hits;
    const allowed = new Set(group.ids);
    return hits.filter((h) => allowed.has(h.class_id));
  });
</script>

<svelte:window onpaste={onPaste} />

<div class="space-y-6">

  <!-- Drop zone + query preview -->
  <section class="grid gap-4 md:grid-cols-[1fr_auto]">
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div
      class="flex min-h-[160px] cursor-pointer flex-col items-center justify-center rounded-lg border-2 border-dashed p-6 text-center transition-colors"
      class:border-baba-accent={dragActive}
      class:bg-baba-panel-2={dragActive}
      class:border-baba-border={!dragActive}
      class:bg-baba-panel={!dragActive}
      onclick={() => fileInput?.click()}
      ondragover={(e) => {
        e.preventDefault();
        dragActive = true;
      }}
      ondragleave={() => (dragActive = false)}
      ondrop={onDrop}
    >
      <div class="text-m font-medium">{t("search_drop_or_pick")}</div>
      <div class="mt-1 text-s text-baba-text-faint">{t("search_drop_hint")}</div>
      <input
        bind:this={fileInput}
        type="file"
        accept="image/jpeg,image/png,image/webp"
        class="hidden"
        onchange={(e) => pickFile((e.currentTarget as HTMLInputElement).files?.[0] ?? null)}
      />
    </div>

    {#if queryPreview}
      <div class="relative">
        <div class="text-s text-baba-text-faint">{t("search_query_preview")}</div>
        <img
          src={queryPreview}
          alt=""
          class="mt-1 h-32 w-32 rounded border border-baba-border object-cover"
        />
        <div class="mt-1 grid"><Button size="small" onclick={clearAll}>{t("search_clear")}</Button></div>
      </div>
    {/if}
  </section>

  <!-- Filters row -->
  {#if queryFile}
    <Card>
      <div class="flex flex-wrap items-end gap-3">
        <label class="flex flex-col gap-1 text-s">
          <span class="text-baba-text-faint">{t("events_filter_all_classes")}</span>
          <select
            bind:value={classGroupFilter}
            onchange={runSearch}
            class="rounded border border-baba-border bg-baba-bg px-2 py-1 text-m"
          >
            <option value="">{t("search_filter_class_all")}</option>
            {#each CLASS_GROUPS as g}
              <option value={g.id}>{t(g.key)}</option>
            {/each}
          </select>
        </label>

        <label class="flex flex-col gap-1 text-s">
          <span class="text-baba-text-faint">{t("events_filter_all_cameras")}</span>
          <select
            bind:value={cameraFilter}
            onchange={runSearch}
            class="rounded border border-baba-border bg-baba-bg px-2 py-1 text-m"
          >
            <option value="">{t("search_filter_camera_all")}</option>
            {#each byLabel(cameras, (c) => c.name) as c (c.id)}
              <option value={c.id}>{c.name}</option>
            {/each}
          </select>
        </label>

        <label class="flex flex-col gap-1 text-s">
          <span class="text-baba-text-faint">
            {t("search_filter_threshold")} {formatNumber(threshold, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
          </span>
          <input
            type="range"
            min="0.1"
            max="1.5"
            step="0.05"
            bind:value={threshold}
            onchange={runSearch}
            class="w-48"
          />
        </label>
        <p class="text-s text-baba-text-faint">{t("search_filter_threshold_hint")}</p>
      </div>
    </Card>
  {/if}

  <!-- Status / results -->
  {#if loading}
    <p class="text-m text-baba-text-muted">{t("search_loading")}</p>
  {:else if error}
    <p class="text-m text-red-400">{t("cameras_error_prefix")}: {error}</p>
  {:else if result && visibleHits.length === 0}
    <p class="text-m text-baba-text-faint">{t("search_no_results")}</p>
  {:else if result}
    <div class="grid gap-3 grid-cols-2 sm:grid-cols-3 md:grid-cols-4 xl:grid-cols-6">
      {#each visibleHits as hit (hit.track_id)}
        <article class="overflow-hidden rounded-lg border border-baba-border bg-baba-panel">
          {#if hit.crop_path}
            <img
              src={thumbnailUrl(hit.crop_path)}
              alt=""
              loading="lazy"
              class="block aspect-square w-full bg-black object-cover"
            />
          {:else if hit.thumbnail_path}
            <img
              src={thumbnailUrl(hit.thumbnail_path)}
              alt=""
              loading="lazy"
              class="block aspect-square w-full bg-black object-cover"
            />
          {:else}
            <div class="grid aspect-square w-full place-items-center bg-baba-panel-2 text-s text-baba-text-faint">
              —
            </div>
          {/if}
          <div class="space-y-1 px-2 py-2 text-s">
            <div class="flex items-center justify-between gap-2">
              <span class="truncate font-medium">{hit.label_name ?? hit.class_name ?? "?"}</span>
              <span class="tabular-nums"><Tag
                tone="quiet"
                title={`${t("search_match_distance")} = ${formatNumber(hit.distance, { minimumFractionDigits: 3, maximumFractionDigits: 3 })}`}
              >{formatNumber(hit.distance, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</Tag></span>
            </div>
            <div class="truncate text-baba-text-faint">
              {hit.camera_name} · {dt.short(hit.started_at)}
            </div>
            {#if hit.global_id}
              <a
                href={`/identities/${hit.global_id}`}
                class="block text-baba-accent hover:underline"
              >{t("search_match_open_identity")} →</a>
            {/if}
          </div>
        </article>
      {/each}
    </div>
  {/if}
</div>
