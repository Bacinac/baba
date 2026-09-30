<script lang="ts">
  // The people of OPUS · Library, for choosing whom to enrol from. One
  // component for both doors in — adding a person to the identities page and
  // adding references on an identity — so the two never drift apart.
  //
  // A person already standing for an identity here is shown but not offered:
  // the library's face is the link, and a second identity for the same person
  // is the over-merge problem in reverse.

  import { api, type OpusPerson } from "$lib/api";
  import { formatNumber, Button, Tag } from "$lib/kit";
  import { t } from "$lib/i18n";
  import { onMount } from "svelte";

  let {
    seed = "",
    busy = false,
    onpick,
    oncancel,
  }: {
    seed?: string;
    busy?: boolean;
    onpick: (p: OpusPerson) => void;
    oncancel: () => void;
  } = $props();

  let loading = $state(true);
  let configured = $state(true);
  let people = $state<OpusPerson[]>([]);
  let filter = $state("");
  let error = $state<string | null>(null);
  let failedCovers = $state<Set<number>>(new Set());

  const matches = $derived(
    filter.trim()
      ? people.filter((p) => p.name.toLowerCase().includes(filter.trim().toLowerCase()))
      : people,
  );

  onMount(async () => {
    filter = seed;
    try {
      const status = await api.opusStatus();
      configured = status.configured;
      if (!status.configured) return;
      people = (await api.opusPeople()).people;
    } catch (e) {
      error = (e as Error).message;
    } finally {
      loading = false;
    }
  });

  function summary(p: OpusPerson): string {
    const faces = t("opus_picker_faces").replace("{n}", formatNumber(p.faces, { maximumFractionDigits: 1 }));
    const [a, b] = p.years;
    if (a === null || a === undefined) return faces;
    return `${faces} · ${a === b ? a : `${a}–${b}`}`;
  }
</script>

{#snippet face(p: OpusPerson)}
  {#if p.cover !== null && !failedCovers.has(p.id)}
    <img
      src={api.opusPersonCoverUrl(p.id)}
      alt=""
      loading="lazy"
      onerror={() => { failedCovers = new Set([...failedCovers, p.id]); }}
      class="h-9 w-9 shrink-0 rounded-full object-cover"
    />
  {:else}
    <span class="h-9 w-9 shrink-0 rounded-full bg-baba-panel"></span>
  {/if}
{/snippet}

<div class="rounded border border-baba-border bg-baba-panel-2 p-3">
  {#if loading}
    <p class="text-m text-baba-text-muted">{t("opus_picker_loading")}</p>
  {:else if !configured}
    <p class="text-m text-amber-400">
      {t("opus_picker_unconfigured")}
      <a href="/settings/face-recognition" class="underline hover:text-amber-300">
        {t("opus_picker_settings_link")}
      </a>
    </p>
  {:else if error}
    <p class="text-m text-red-400">{error}</p>
  {:else}
    <div class="mb-2 flex items-center gap-2">
      <input
        id="opus-picker-filter"
        type="search"
        bind:value={filter}
        placeholder={t("opus_picker_search")}
        class="w-full rounded border border-baba-border bg-baba-panel px-2 py-1.5 text-m"
      />
      <div class="shrink-0"><Button onclick={oncancel}>{t("common.cancel")}</Button></div>
    </div>
    {#if matches.length === 0}
      <p class="text-m text-baba-text-faint">{t("opus_picker_none")}</p>
    {:else}
      <ul class="max-h-72 divide-y divide-baba-border overflow-y-auto">
        {#each matches as p (p.id)}
          <li>
            {#if p.identity}
              <a
                href="/identities/{p.identity}"
                title={t("opus_picker_linked")}
                class="flex w-full items-center gap-3 px-2 py-1.5 text-left text-m text-baba-text-muted hover:bg-baba-panel"
              >
                {@render face(p)}
                <span class="min-w-0 flex-1 truncate">{p.name}</span>
                <span class="shrink-0 text-s text-baba-text-faint">{summary(p)}</span>
                <span class="shrink-0 text-s text-baba-accent underline">{t("opus_picker_linked_link")}</span>
              </a>
            {:else}
              <button
                type="button"
                onclick={() => onpick(p)}
                disabled={busy}
                title={p.same_name ? t("opus_picker_same_name_hint") : ""}
                class="flex w-full items-center gap-3 px-2 py-1.5 text-left text-m hover:bg-baba-panel disabled:opacity-50"
              >
                {@render face(p)}
                <span class="min-w-0 flex-1 truncate">{p.name}</span>
                <span class="shrink-0 text-s text-baba-text-faint">{summary(p)}</span>
                {#if p.same_name}
                  <span class="shrink-0"><Tag tone="busy">
                    {t("opus_picker_same_name")}
                  </Tag></span>
                {/if}
              </button>
            {/if}
          </li>
        {/each}
      </ul>
    {/if}
  {/if}
</div>
