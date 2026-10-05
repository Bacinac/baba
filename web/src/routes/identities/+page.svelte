<script lang="ts">
  import { onDestroy, onMount, untrack } from "svelte";
  import { goto } from "$app/navigation";
  import { api, identitiesFeed, thumbnailUrl,
    type IdentitiesFilter, type IdentitySummary, type OpusPerson, type ReferenceSources,
    type Feed,
  } from "$lib/api";
  import OpusPeoplePicker from "$lib/OpusPeoplePicker.svelte";
  import { dialog, plural, Button, Dialog, Heading, Picks, Tag, PageActions } from "$lib/kit";
  import { t, type MessageKey } from "$lib/i18n";
  import { identitySections, sectionOfClass, type IdentitySection } from "$lib/identities";
  import { dt } from "$lib/datetime.svelte";
  import { classLabel } from "$lib/classLabels";

  // Filter chips. `class_id` matches the canonical id of each group;
  // backend expands to the full re-ID class group (e.g. clicking
  // "Vozila" passes class_id=2 and the server returns car+truck+bus+
  // motorcycle). null = no class filter at all.
  type ClassChip = {
    key: string;
    label_key: "identities_filter_all" | "identities_filter_person"
             | "identities_filter_vehicle" | "identities_filter_pet";
    class_id: number | null;
  };
  // Birds were considered but cut — random outdoor sightings, no
  // identity continuity worth modelling. Detector still tracks them
  // as raw objects in /events; just not surfaced here.
  const CLASS_CHIPS: ClassChip[] = [
    { key: "all",     label_key: "identities_filter_all",     class_id: null },
    { key: "person",  label_key: "identities_filter_person",  class_id: 0 },
    { key: "vehicle", label_key: "identities_filter_vehicle", class_id: 2 },
    { key: "pet",     label_key: "identities_filter_pet",     class_id: 15 },
  ];

  let identities = $state<IdentitySummary[]>([]);
  let loading = $state(true);
  let error = $state<string | null>(null);
  let feed: Feed | null = null;
  let refreshTimer: ReturnType<typeof setTimeout> | null = null;

  let classChip = $state<string>("all");
  let query = $state("");
  let queryDebounceTimer: ReturnType<typeof setTimeout> | null = null;

  function currentFilter(): IdentitiesFilter {
    const chip = CLASS_CHIPS.find(c => c.key === classChip)!;
    const f: IdentitiesFilter = { limit: 200, has_image: true };
    if (chip.class_id !== null) f.class_id = chip.class_id;
    if (query.trim()) f.q = query.trim();
    return f;
  }

  // Monotonic guard so overlapping loads (chip change racing a debounced
  // query load) settle deterministically — newest wins.
  let loadSeq = 0;

  // Similarity groups over the unnamed clusters (server-side connected
  // components on the same embeddings the `near` kNN uses). Rendered as one
  // stack per group so "this person's 30 scattered cards" is ONE assign.
  let anonGroupIds = $state<string[][]>([]);

  async function load(silent = false) {
    const myseq = ++loadSeq;
    if (!silent) loading = true;
    error = null;
    try {
      const [rows, grp] = await Promise.all([
        api.listIdentities(currentFilter()),
        api.anonGroups().catch(() => ({ groups: [] as string[][] })),
      ]);
      if (myseq !== loadSeq) return; // superseded
      identities = rows;
      anonGroupIds = grp.groups;
    } catch (e) {
      if (myseq === loadSeq) error = (e as Error).message;
    } finally {
      if (!silent && myseq === loadSeq) loading = false;
    }
  }

  // Re-fetch when the class chip changes. `query` is deliberately NOT a
  // dependency: load() reads it via currentFilter(), so tracking it here would
  // fire an extra un-debounced fetch on every keystroke on top of the debounced
  // one in onQueryInput. untrack() keeps the query read out of the effect's
  // dependency set.
  $effect(() => {
    void classChip;
    untrack(() => void load());
  });

  function onQueryInput(e: Event) {
    query = (e.currentTarget as HTMLInputElement).value;
    if (queryDebounceTimer) clearTimeout(queryDebounceTimer);
    queryDebounceTimer = setTimeout(() => { void load(); }, 250);
  }

  // What a card's class chip SAYS: the identity owns what it is. Species wins
  // over the detector's per-frame flip (Franka is a cat however often the
  // detector says dog); a DEVICE never shows its detector class at all — the
  // class is a known misread (the robot mower reads as "dog"; that is exactly
  // why the operator marked it a device). Mirrors the detail page's rule.
  function cardClass(id: IdentitySummary): string {
    // The stored value is a lowercased English token ("dog", "cat"); the
    // chip beside it goes through the translated dictionary, so this one read
    // English in a Croatian UI.
    if (id.label?.species) return classLabel(id.label.species);
    if (id.label?.kind?.toLowerCase() === "object") return t("identities_add_kind_object");
    return id.class_name;
  }

  // Tracked = has an operator label. Anon = auto-clustered, never named.
  // The page is for managing the *tracked* set; anon entries sit in a
  // collapsed section below so daily car / bird sightings don't bury
  // the small handful of named people we actually care about.
  let named   = $derived(identities.filter(i => i.label));
  let namedSections = $derived(identitySections(named));
  const SECTION_LABEL: Record<IdentitySection, MessageKey> = {
    person:  "identities_filter_person",
    vehicle: "identities_filter_vehicle",
    pet:     "identities_filter_pet",
    other:   "identities_add_kind_other",
  };
  // Groups materialised against the CURRENT anon card set (filters apply):
  // a group renders only when ≥2 of its members are visible; its remaining
  // members render as ordinary singles.
  let anonGrouped = $derived.by(() => {
    const bySumm = new Map(identities.filter(i => !i.label).map(i => [i.global_id, i]));
    return anonGroupIds
      .map(g => g.map(gid => bySumm.get(gid)).filter((x): x is IdentitySummary => !!x && !!x.crop_path))
      .filter(m => m.length >= 2);
  });
  let groupedGids = $derived(new Set(anonGrouped.flat().map(i => i.global_id)));
  // Group-assign flow: pick a group → pick the named identity → fold every
  // member in (sequential merges; the endpoint moves tracks + photos).
  let groupAssign = $state<IdentitySummary[] | null>(null);
  let groupCandidates = $state<IdentitySummary[]>([]);
  let groupCandLoading = $state(false);
  let groupAssignBusy = $state(false);
  let expandedGroups = $state<Set<number>>(new Set());

  function toggleGroupExpand(idx: number) {
    const n = new Set(expandedGroups);
    if (n.has(idx)) n.delete(idx);
    else n.add(idx);
    expandedGroups = n;
  }

  async function openGroupAssign(members: IdentitySummary[]) {
    groupAssign = members;
    groupCandLoading = true;
    try {
      groupCandidates = await api.listIdentities({
        class_id: members[0].class_id,
        near: members[0].global_id,
        labeled: true,
        limit: 24,
      });
    } catch (e) {
      error = (e as Error).message;
    } finally {
      groupCandLoading = false;
    }
  }

  // Wipe the whole unnamed backlog. 202 + background purge server-side; the
  // SSE refresh shrinks the list live as it lands.
  let deleteAllBusy = $state(false);
  async function doDeleteAllAnon() {
    if (deleteAllBusy) return;
    const ok = await dialog.confirm({
      title: t("identities_delete_all_button"),
      message: t("identities_delete_all_confirm"),
      confirmLabel: t("dialog_delete"),
      danger: true,
    });
    if (!ok) return;
    deleteAllBusy = true;
    try {
      await api.deleteAllAnon();
      // The purge runs server-side in the background (202). FEEDBACK: keep the
      // button in its busy state and poll until the backlog actually reads
      // empty — the count on the button shrinks live. Give up after ~45 s
      // (a huge backlog keeps purging server-side; the list just refreshes on
      // the next visit).
      for (let i = 0; i < 30 && anon.length > 0; i++) {
        await new Promise((res) => setTimeout(res, 1500));
        await load(true);
      }
    } catch (e) {
      error = (e as Error).message;
    } finally {
      deleteAllBusy = false;
    }
  }

  // Delete a whole group — junk clusters (mirrors, road passers-by) come in
  // batches too; purging them one card at a time is the same dance the group
  // assign exists to avoid. Full purge per member (label/refs/tracks/files).
  let groupDeleteBusy = $state(false);
  async function doGroupDelete(members: IdentitySummary[]) {
    if (groupDeleteBusy) return;
    const ok = await dialog.confirm({
      title: t("identities_delete_button"),
      message: t("identities_group_delete_confirm").replace("{n}", String(members.length)),
      confirmLabel: t("dialog_delete"),
      danger: true,
    });
    if (!ok) return;
    groupDeleteBusy = true;
    try {
      for (const m of members) {
        await api.deleteIdentity(m.global_id);
      }
      await load(true);
    } catch (e) {
      error = (e as Error).message;
    } finally {
      groupDeleteBusy = false;
    }
  }

  async function doGroupAssign(target: IdentitySummary) {
    if (!groupAssign || groupAssignBusy) return;
    const name = target.label?.name ?? "?";
    const ok = await dialog.confirm({
      title: t("identities_assign_title"),
      message: t("identities_assign_group_confirm")
        .replace("{n}", String(groupAssign.length))
        .replace("{name}", name),
    });
    if (!ok) return;
    groupAssignBusy = true;
    try {
      for (const m of groupAssign) {
        await api.mergeIdentity(m.global_id, target.global_id);
      }
      groupAssign = null;
      await load(true);
    } catch (e) {
      error = (e as Error).message;
    } finally {
      groupAssignBusy = false;
    }
  }
  // Only surface unnamed clusters that actually have a focused crop to show —
  // image-less singletons are un-reviewable noise.
  let anon    = $derived(identities.filter(i => !i.label && i.crop_path));
  let anonByClass = $derived.by(() => {
    const m = new Map<string, IdentitySummary[]>();
    for (const id of anon) {
      const k = id.class_name ?? "?";
      const list = m.get(k) ?? [];
      list.push(id);
      m.set(k, list);
    }
    // Stable order — most common class first.
    return Array.from(m.entries()).sort((a, b) => b[1].length - a[1].length);
  });
  let showAnon = $state(false);

  function focusAnonClass(className: string) {
    const section = sectionOfClass(className);
    classChip = section === "other" ? "all" : section;
    showAnon = true;
  }

  function scheduleRefresh() {
    // Debounce: bursty auto_match events during heavy traffic shouldn't
    // trigger one refetch per row. Coalesce ~1s of arrivals into a single
    // background refresh that keeps the page mounted (no spinner flash).
    if (refreshTimer !== null) clearTimeout(refreshTimer);
    refreshTimer = setTimeout(() => {
      refreshTimer = null;
      void load(true);
    }, 1000);
  }

  onDestroy(() => {
    if (queryDebounceTimer !== null) clearTimeout(queryDebounceTimer);
  });

  function sightingsLabel(n: number): string {
    return plural(
      n,
      "identities_n_sightings_one",
      "identities_n_sightings_few",
      "identities_n_sightings_many",
    );
  }

  // ---- Add-identity modal --------------------------------------------------
  // Lives as inline state — single-purpose, single-page, no need for a
  // generic modal abstraction. Client generates the gid (UUID v4) and
  // calls PUT label + POST reference-photos in sequence; if the photo
  // step fails, the empty identity card remains so the operator can
  // retry the upload from the detail page.
  type KindKey = "person" | "vehicle" | "pet" | "other";
  const KIND_CHIPS: { key: KindKey; label_key:
    "identities_add_kind_person" | "identities_add_kind_vehicle"
    | "identities_add_kind_pet" | "identities_add_kind_other" }[] = [
    { key: "person",  label_key: "identities_add_kind_person" },
    { key: "vehicle", label_key: "identities_add_kind_vehicle" },
    { key: "pet",     label_key: "identities_add_kind_pet" },
    { key: "other",   label_key: "identities_add_kind_other" },
  ];
  // Relationship chips. These used to write Croatian strings into `tags`
  // (multi-select), which drifted immediately: one identity ended up with both
  // `obitelj` and `family`, another with only `obitelj`, so filtering "family"
  // silently missed it. Now they set the structured `affiliation` column —
  // single-select, English in the DB, localised here. Applies to pets too (a
  // neighbour's cat is not ours).
  const AFFILIATION_CHIPS: { key: string; label_key:
    "identities_affiliation_family" | "identities_affiliation_friend"
    | "identities_affiliation_neighbour" | "identities_affiliation_guest"
    | "identities_affiliation_delivery" | "identities_affiliation_service"
    | "identities_affiliation_official" | "identities_affiliation_unknown" }[] = [
    { key: "family",    label_key: "identities_affiliation_family" },
    { key: "friend",    label_key: "identities_affiliation_friend" },
    { key: "neighbour", label_key: "identities_affiliation_neighbour" },
    { key: "guest",     label_key: "identities_affiliation_guest" },
    { key: "delivery",  label_key: "identities_affiliation_delivery" },
    { key: "service",   label_key: "identities_affiliation_service" },
    { key: "official",  label_key: "identities_affiliation_official" },
    { key: "unknown",   label_key: "identities_affiliation_unknown" },
  ];

  let showAddModal = $state(false);
  let addName = $state("");
  let addKind = $state<KindKey>("person");
  let addAffiliation = $state<string>("unknown");
  // Orthogonal to affiliation, not a value of it: Nika is family but no
  // longer lives here; a lodger is resident without being family.
  let addResident = $state(false);
  let addSpecies = $state("");
  let addFiles = $state<File[]>([]);
  let addFileInput = $state<HTMLInputElement | undefined>();
  let addBusy = $state<"idle" | "creating" | "uploading">("idle");
  let addError = $state<string | null>(null);

  function openAddModal(): void {
    addName = "";
    addKind = "person";
    addAffiliation = "unknown";
    addResident = false;
    addSpecies = "";
    addFiles = [];
    addBusy = "idle";
    addError = null;
    showAddModal = true;
  }
  function closeAddModal(): void {
    if (addBusy !== "idle") return;
    showAddModal = false;
  }
  function onAddFiles(e: Event): void {
    const f = (e.currentTarget as HTMLInputElement).files;
    addFiles = f ? Array.from(f) : [];
  }
  // RFC 4122 v4 UUID. We can't rely on `crypto.randomUUID` because the
  // BABA UI is normally accessed over plain HTTP on a LAN IP, and that
  // method is only exposed in secure contexts (HTTPS / localhost).
  // `crypto.getRandomValues` works everywhere a modern browser runs.
  function uuidv4(): string {
    if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
      return crypto.randomUUID();
    }
    const buf = new Uint8Array(16);
    crypto.getRandomValues(buf);
    buf[6] = (buf[6] & 0x0f) | 0x40;
    buf[8] = (buf[8] & 0x3f) | 0x80;
    const hex = Array.from(buf, b => b.toString(16).padStart(2, "0")).join("");
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }

  async function submitAdd(): Promise<void> {
    addError = null;
    const name = addName.trim();
    if (!name) {
      addError = t("identities_add_err_no_name");
      return;
    }
    const gid = uuidv4();
    addBusy = "creating";
    try {
      await api.upsertIdentityLabel(gid, {
        name,
        kind: addKind === "other" ? null : addKind,
        affiliation: addAffiliation,
        resident: addResident,
        // Species is a pet fact; don't carry a stale value if the operator
        // switched the kind chip after typing one.
        species: addKind === "pet" && addSpecies.trim() ? addSpecies.trim().toLowerCase() : null,
      });
      if (addFiles.length > 0) {
        addBusy = "uploading";
        await api.uploadReferencePhotos(gid, addFiles);
      }
      showAddModal = false;
      addBusy = "idle";
      void goto(`/identities/${gid}`);
    } catch (e) {
      addError = (e as Error).message;
      addBusy = "idle";
    }
  }

  // ---- Add from OPUS · Library ---------------------------------------------
  // A library person becomes an identity: named as the library names them,
  // enrolled from their photographs in the same call. The way a person is
  // first added; uploads remain for pets, vehicles and strangers.
  let showOpusModal = $state(false);
  let opusBusy = $state(false);
  let opusError = $state<string | null>(null);
  let sources = $state<ReferenceSources | null>(null);
  onMount(async () => {
    try { sources = await api.referenceSources(); } catch { sources = null; }
  });

  function openOpusModal(): void {
    opusError = null;
    showOpusModal = true;
  }
  function closeOpusModal(): void {
    if (opusBusy) return;
    showOpusModal = false;
  }
  async function addFromOpus(p: OpusPerson): Promise<void> {
    if (opusBusy) return;
    opusBusy = true;
    opusError = null;
    try {
      // A same-named identity already here is the person: link and enrol
      // it, never a twin.
      const r = p.same_name
        ? { ...(await api.referencePhotosFromOpus(p.same_name, { person_id: p.id })), global_id: p.same_name }
        : await api.identityFromOpus({ person_id: p.id });
      showOpusModal = false;
      if (r.used === 0) {
        await dialog.alert({
          title: t("identities_add_opus_title"),
          message: `${t("identities_add_opus_none_used")}\n${r.skipped.join("\n")}`,
        });
      }
      void goto(`/identities/${r.global_id}`);
    } catch (e) {
      opusError = (e as Error).message;
    } finally {
      opusBusy = false;
    }
  }

  // ---- Delete-identity (card-level) ---------------------------------------
  let deleting = $state<Set<string>>(new Set());
  // Identities whose card image failed to load (e.g. a reference photo
  // whose file is gone — embeddings live in Postgres but the JPEG can
  // be lost in a media move). Fall back to the neutral placeholder
  // instead of a broken black tile.
  let imgFailed = $state<Set<string>>(new Set());
  async function onDeleteCard(ev: Event, id: IdentitySummary): Promise<void> {
    ev.preventDefault();
    ev.stopPropagation();
    const msg = id.label
      ? t("identities_delete_confirm").replace("{name}", id.label.name)
      : t("identities_delete_confirm_anon");
    const ok = await dialog.confirm({
      title: t("identities_delete_confirm_title"),
      message: msg,
      confirmLabel: t("dialog_delete"),
      danger: true,
    });
    if (!ok) return;
    const next = new Set(deleting); next.add(id.global_id); deleting = next;
    try {
      await api.deleteIdentity(id.global_id);
      identities = identities.filter(x => x.global_id !== id.global_id);
    } catch (e) {
      await dialog.alert({
        title: t("dialog_error_title"),
        message: (e as Error).message,
      });
    } finally {
      const off = new Set(deleting); off.delete(id.global_id); deleting = off;
    }
  }

  onMount(() => {
    // Initial load is owned by the classChip $effect (runs once on mount);
    // calling load() here too double-fetched on first paint.
    feed = identitiesFeed({ message: scheduleRefresh, resync: scheduleRefresh });
  });

  onDestroy(() => {
    if (refreshTimer !== null) clearTimeout(refreshTimer);
    feed?.close();
  });
</script>

<PageActions>
  {#if sources?.opus}
    <Button tone="accent" onclick={openOpusModal}>{t("identities_add_opus_button")}</Button>
  {/if}
  <Button tone={sources?.opus ? "quiet" : "accent"} onclick={openAddModal}>{t("identities_add_button")}</Button>
</PageActions>

<div class="mb-4 flex flex-wrap items-center gap-2">
  <Picks picks={CLASS_CHIPS.map((c) => ({ key: c.key, label: t(c.label_key) }))} chosen={[classChip]} onpick={(k) => (classChip = k)} />
  <input
    type="text"
    value={query}
    oninput={onQueryInput}
    placeholder={t("identities_search_placeholder")}
    class="ml-auto w-56 rounded border border-baba-border bg-baba-panel-2 px-2 py-1 text-m focus:border-baba-accent focus:outline-none"
  />
</div>

{#snippet card(id: IdentitySummary, compact: boolean = false)}
  <li class="group relative">
    <a
      href={`/identities/${id.global_id}`}
      class="block overflow-hidden rounded-lg border border-baba-border bg-baba-panel hover:bg-baba-panel-2"
      class:opacity-50={deleting.has(id.global_id)}
    >
      <div class="relative aspect-square bg-black">
        {#if (id.crop_path || id.thumbnail_path) && !imgFailed.has(id.global_id)}
          <img
            src={thumbnailUrl((id.crop_path ?? id.thumbnail_path) as string)}
            alt={cardClass(id)}
            class="h-full w-full object-cover"
            loading="lazy"
            onerror={() => { const n = new Set(imgFailed); n.add(id.global_id); imgFailed = n; }}
          />
        {:else}
          <div class="grid h-full place-items-center text-s text-baba-text-faint">
            {t("identities_no_thumb")}
          </div>
        {/if}
        <span class="absolute left-2 top-2"><Tag tone="busy" onpicture>
          {cardClass(id)}
        </Tag></span>
        <span class="absolute right-2 bottom-2"><Tag onpicture>
          {id.n_tracks} {sightingsLabel(id.n_tracks)}
        </Tag></span>
        <!-- Provenance: only meaningful for people (a car/pet has no face).
             Face-confirmed = trustworthy across sessions; appearance-only =
             grouped by body/clothing, weaker. -->
        {#if id.class_id === 0}
          {#if id.face_confirmed}
            <span class="absolute left-2 bottom-2"><Tag tone="ok" onpicture title={t("identities_face_confirmed_hint")}>{t("identities_face_confirmed_badge")}</Tag></span>
          {:else}
            <span class="absolute left-2 bottom-2"><Tag tone="busy" onpicture title={t("identities_appearance_only_hint")}>{t("identities_appearance_only_badge")}</Tag></span>
          {/if}
        {/if}
      </div>
      <div class="space-y-1 p-3 text-s" class:!p-2={compact}>
        <div class="flex items-center gap-1 truncate font-medium text-baba-text">
          {#if id.label}
            <span class="truncate">{id.label.name}</span>
            {#if id.label.source === "ai"}
              <span class="shrink-0 rounded bg-baba-accent/15 px-1 text-2xs font-medium text-baba-accent"
                    title="AI suggestion, not reviewed yet">✨</span>
            {/if}
            {#if id.label.has_reference_embedding}
              <span class="shrink-0 text-baba-accent" title="enrolled with reference photos">★</span>
            {/if}
          {:else}
            <span class="text-baba-text-faint">{t("identities_unnamed")}</span>
          {/if}
        </div>
        {#if id.label?.plate}
          <div class="text-baba-text-muted font-mono text-xs">{id.label.plate}</div>
        {/if}
        <div class="text-baba-text-muted">
          {dt.short(id.last_seen)}
        </div>
        {#if !compact}
          <div class="text-baba-text-faint truncate">
            {id.cameras.join(" · ")}
          </div>
        {/if}
      </div>
    </a>
    <button
      type="button"
      onclick={(e) => onDeleteCard(e, id)}
      disabled={deleting.has(id.global_id)}
      title={t("identities_delete_card_tooltip")}
      aria-label={t("identities_delete_card_tooltip")}
      class="absolute right-2 top-2 grid h-6 w-6 place-items-center rounded-full
             border border-red-500/40 bg-black/70 text-s text-red-300
             opacity-0 transition-opacity hover:bg-red-500/40 hover:text-white
             group-hover:opacity-100 focus:opacity-100 disabled:cursor-not-allowed"
    >×</button>
  </li>
{/snippet}

{#if loading}
  <p class="text-baba-text-faint">{t("identities_loading")}</p>
{:else if error}
  <p class="text-red-400">{error}</p>
{:else if identities.length === 0}
  <p class="text-baba-text-faint">{t("identities_empty")}</p>
{:else}
  <section class="mb-8">
    {#if named.length === 0}
      <Heading label={t("identities_section_tracked")} />
      <p class="text-s text-baba-text-faint">{t("identities_section_tracked_empty")}</p>
    {:else}
      {#each namedSections as [section, list] (section)}
        <Heading label={t(SECTION_LABEL[section])} count={list.length} />
        <ul class="mb-6 grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5">
          {#each list as id (id.global_id)}
            {@render card(id, false)}
          {/each}
        </ul>
      {/each}
    {/if}
  </section>

  <!-- Auto-detected, unnamed: collapsed by default, grouped by class. -->
  {#if anon.length > 0}
    <section class="mt-6 rounded-lg border border-baba-border bg-baba-panel-2/40 p-4">
      <div class="flex w-full items-center gap-2 text-m font-semibold text-baba-text">
        <button
          type="button"
          onclick={() => (showAnon = !showAnon)}
          class="flex items-center gap-2 text-left hover:text-baba-accent"
        >
          <span class="inline-block w-3 text-baba-text-muted">{showAnon ? "▾" : "▸"}</span>
          {t("identities_section_anon")}
          <span class="text-s font-normal text-baba-text-faint">({anon.length})</span>
        </button>
        <span class="ml-auto flex flex-wrap items-center gap-2 text-s font-normal text-baba-text-muted">
          <Button tone="danger" size="small" onclick={doDeleteAllAnon} disabled={deleteAllBusy}>{deleteAllBusy
            ? t("identities_delete_all_busy").replace("{n}", String(anon.length))
            : t("identities_delete_all_button")}</Button>
          {#each anonByClass as [klass, list] (klass)}
            <Button size="small" onclick={() => focusAnonClass(klass)}>
              {klass} · {list.length}
            </Button>
          {/each}
        </span>
      </div>
      {#if showAnon}
        {#if anonGrouped.length > 0}
          <!-- Similarity groups first: one stack + one "Ovo je…" per subject
               instead of N scattered single cards. -->
          <div class="mt-4 space-y-2">
            {#each anonGrouped as members, gi (members[0].global_id)}
              <div class="rounded-lg border border-baba-border bg-baba-panel p-2">
                <div class="flex items-center gap-3">
                  <a href={`/identities/${members[0].global_id}`} aria-label={t("search_match_open_identity")} class="block h-16 w-16 shrink-0 overflow-hidden rounded bg-black">
                    <img src={thumbnailUrl(members[0].crop_path as string)} alt="" class="h-full w-full object-cover" loading="lazy" />
                  </a>
                  <div class="min-w-0 flex-1">
                    <div class="text-m font-medium">
                      {t("identities_group_similar").replace("{n}", String(members.length))}
                      <span class="ml-1 text-s font-normal text-baba-text-faint">{classLabel(members[0].class_name)}</span>
                    </div>
                    <div class="text-s text-baba-text-faint">{dt.short(members[0].last_seen)}</div>
                  </div>
                  <Button size="small" onclick={() => toggleGroupExpand(gi)}>{expandedGroups.has(gi) ? "▾" : "▸"}</Button>
                  <Button tone="accent" size="small" onclick={() => openGroupAssign(members)}>{t("identities_assign_button")}</Button>
                  <Button tone="danger" size="small" onclick={() => doGroupDelete(members)} disabled={groupDeleteBusy}>{t("dialog_delete")}</Button>
                </div>
                {#if expandedGroups.has(gi)}
                  <ul class="mt-2 flex flex-wrap gap-2">
                    {#each members as m (m.global_id)}
                      <li>
                        <a href={`/identities/${m.global_id}`} aria-label={t("search_match_open_identity")} class="block h-20 w-20 overflow-hidden rounded border border-baba-border bg-black hover:border-baba-accent/50">
                          <img src={thumbnailUrl(m.crop_path as string)} alt="" class="h-full w-full object-cover" loading="lazy" />
                        </a>
                      </li>
                    {/each}
                  </ul>
                {/if}
              </div>
            {/each}
          </div>
        {/if}
        <ul class="mt-4 grid grid-cols-3 gap-3 sm:grid-cols-4 lg:grid-cols-6 xl:grid-cols-8">
          {#each anon.filter((i) => !groupedGids.has(i.global_id)) as id (id.global_id)}
            {@render card(id, true)}
          {/each}
        </ul>
      {/if}
    </section>
  {/if}

{/if}

{#if groupAssign}
  <Dialog title={t("identities_assign_title")} onclose={() => { if (!groupAssignBusy) groupAssign = null; }}>
      <p class="text-s text-baba-text-faint">{t("identities_assign_hint")}</p>
      {#if groupCandLoading}
        <p class="mt-4 text-m text-baba-text-faint">{t("identities_merge_loading")}</p>
      {:else if groupCandidates.length === 0}
        <p class="mt-4 text-m text-baba-text-faint">{t("identities_assign_empty")}</p>
      {:else}
        <ul class="mt-4 grid grid-cols-3 gap-3 sm:grid-cols-4 lg:grid-cols-6">
          {#each groupCandidates as c (c.global_id)}
            <li>
              <button
                type="button"
                onclick={() => doGroupAssign(c)}
                disabled={groupAssignBusy}
                class="block w-full overflow-hidden rounded border border-baba-border bg-baba-panel-2 text-left hover:border-baba-accent/50 hover:bg-baba-panel disabled:opacity-50"
              >
                <div class="aspect-square bg-black">
                  {#if c.crop_path || c.thumbnail_path}
                    <img
                      src={thumbnailUrl((c.crop_path ?? c.thumbnail_path) as string)}
                      alt={c.label?.name ?? c.class_name}
                      class="h-full w-full object-cover"
                      loading="lazy"
                    />
                  {/if}
                </div>
                <div class="truncate px-2 py-1.5 text-s font-medium">{c.label?.name}</div>
              </button>
            </li>
          {/each}
        </ul>
      {/if}
  </Dialog>
{/if}

{#if showAddModal}
  <Dialog title={t("identities_add_title")} size="narrow" onclose={closeAddModal}>
      <p class="text-s text-baba-text-faint">{t("identities_add_desc")}</p>

      <div class="mt-4 space-y-3">
        <label class="block">
          <span class="block text-s text-baba-text-muted">{t("identities_label_name")}</span>
          <input
            type="text"
            bind:value={addName}
            placeholder={t("identities_label_name_placeholder")}
            class="mt-1 w-full rounded border border-baba-border bg-baba-panel-2 px-2 py-1.5 text-m focus:border-baba-accent focus:outline-none"
            {@attach (el: HTMLInputElement) => { el.focus(); }}
          />
        </label>

        <div>
          <span class="block text-s text-baba-text-muted">{t("identities_label_kind")}</span>
          <div class="mt-1 flex flex-wrap gap-1.5">
            <Picks picks={KIND_CHIPS.map((k) => ({ key: k.key, label: t(k.label_key) }))} chosen={[addKind]} onpick={(k) => (addKind = k as KindKey)} />
          </div>
        </div>

        <div>
          <span class="block text-s text-baba-text-muted">{t("identities_label_affiliation")}</span>
          <div class="mt-1 flex flex-wrap gap-1.5">
            <Picks picks={AFFILIATION_CHIPS.map((a) => ({ key: a.key, label: t(a.label_key) }))} chosen={[addAffiliation]} onpick={(k) => (addAffiliation = k)} />
          </div>
        </div>

        {#if addKind === "person" || addKind === "pet"}
          <label class="flex cursor-pointer items-center gap-2 text-s text-baba-text-muted">
            <input type="checkbox" bind:checked={addResident} class="accent-baba-accent" />
            {t("identities_label_resident")}
          </label>
        {/if}

        {#if addKind === "pet"}
          <label class="block">
            <span class="block text-s text-baba-text-muted">{t("identities_label_species")}</span>
            <input
              bind:value={addSpecies}
              placeholder="cat / dog"
              class="mt-1 w-full rounded border border-baba-border bg-baba-panel-2 px-2 py-1.5 text-m focus:border-baba-accent focus:outline-none"
            />
          </label>
        {/if}

        <div>
          <label class="block">
            <span class="block text-s text-baba-text-muted">{t("identities_add_photos_label")}</span>
            <input
              type="file"
              accept="image/jpeg,image/png"
              multiple
              bind:this={addFileInput}
              onchange={onAddFiles}
              class="mt-1 block w-full text-s text-baba-text-muted
                     file:mr-3 file:rounded file:border file:border-baba-border
                     file:bg-baba-panel-2 file:px-3 file:py-1.5 file:text-s
                     file:text-baba-text-muted hover:file:bg-baba-panel"
            />
          </label>
          <p class="mt-1 text-xs text-baba-text-faint">{t("identities_add_photos_hint")}</p>
          {#if addFiles.length > 0}
            <p class="mt-1 text-xs text-baba-accent">
              {addFiles.length} × {addFiles.map(f => f.name).join(", ")}
            </p>
          {/if}
        </div>

        {#if addError}
          <p class="text-s text-red-400">{addError}</p>
        {/if}
      </div>

      <div class="mt-5 flex justify-end gap-2">
        <Button size="small" onclick={closeAddModal} disabled={addBusy !== "idle"}>{t("common.cancel")}</Button>
        <Button tone="accent" size="small" onclick={submitAdd} disabled={addBusy !== "idle"}>
          {#if addBusy === "creating"}{t("identities_add_busy")}
          {:else if addBusy === "uploading"}{t("identities_add_uploading")}
          {:else}{t("identities_add_submit")}{/if}
        </Button>
      </div>
  </Dialog>
{/if}

{#if showOpusModal}
  <Dialog title={t("identities_add_opus_title")} size="narrow" onclose={closeOpusModal}>
      <p class="text-s text-baba-text-faint">{t("identities_add_opus_desc")}</p>
      <div class="mt-4">
        <OpusPeoplePicker busy={opusBusy} onpick={addFromOpus} oncancel={closeOpusModal} />
      </div>
      {#if opusBusy}
        <p class="mt-3 text-s text-baba-text-muted">{t("identities_add_opus_busy")}</p>
      {/if}
      {#if opusError}
        <p class="mt-3 text-s text-red-400">{opusError}</p>
      {/if}
  </Dialog>
{/if}
