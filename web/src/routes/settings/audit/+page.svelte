<script lang="ts">
  import { byLabel } from "$lib/order";
  import { api, type AuditEntry, type AuditFacets } from "$lib/api";
  import { plural, Tag, type TagTone, Button, Dialog, Card } from "$lib/kit";
  import { t, type MessageKey } from "$lib/i18n";
  import { dt } from "$lib/datetime.svelte";
  import { auth } from "$lib/auth.svelte";

  let entries = $state<AuditEntry[]>([]);
  let loading = $state(true);
  let loadingMore = $state(false);
  let hasMore = $state(true);
  let error = $state<string | null>(null);

  // Filters — null/"" = "all", same shape as events page.
  let typeFilter = $state("");
  let opFilter = $state("");
  let facets = $state<AuditFacets>({ resource_types: [], ops: [] });

  // Expand-payload state (per-row). Keyed by audit row id so toggling
  // one row doesn't collapse another the user explicitly opened.
  let expanded = $state<Record<string, boolean>>({});

  const PAGE_SIZE = 200;

  const PURGE_WINDOWS: { days: number; key: MessageKey }[] = [
    { days: 30,  key: "audit_purge_30d" },
    { days: 90,  key: "audit_purge_90d" },
    { days: 180, key: "audit_purge_180d" },
    { days: 365, key: "audit_purge_365d" },
  ];

  // Purge dialog state.
  let purgeOpen = $state(false);
  let purgeDays = $state(90);
  let purgeRunning = $state(false);
  let purgeResult = $state<string | null>(null);

  const resourceLabel = (id: string) => t(`audit_resource_${id}` as MessageKey);
  const opLabel = (op: string) => t(`audit_op_${op}` as MessageKey);

  const OP_TONE: Record<string, TagTone> = {
    create: "ok",
    capture: "ok",
    delete: "err",
    prototype_delete: "err",
    purge: "busy",
    dismiss: "quiet",
  };

  // Guard against out-of-order responses when filters toggle quickly.
  let refreshSeq = 0;

  async function refresh() {
    const myseq = ++refreshSeq;
    loading = true;
    error = null;
    try {
      const [rows, held] = await Promise.all([
        api.listAudit({
          resource_type: typeFilter || undefined,
          op: opFilter || undefined,
          limit: PAGE_SIZE,
        }),
        api.auditFacets(),
      ]);
      if (myseq !== refreshSeq) return;
      entries = rows;
      facets = held;
      hasMore = entries.length >= PAGE_SIZE;
      expanded = {};
    } catch (e) {
      if (myseq === refreshSeq) error = e instanceof Error ? e.message : String(e);
    } finally {
      if (myseq === refreshSeq) loading = false;
    }
  }

  async function loadMore() {
    if (loadingMore || entries.length === 0) return;
    loadingMore = true;
    try {
      const cursor = entries[entries.length - 1].at;
      const page = await api.listAudit({
        resource_type: typeFilter || undefined,
        op: opFilter || undefined,
        until: cursor,
        limit: PAGE_SIZE,
      });
      if (page.length === 0) {
        hasMore = false;
        return;
      }
      const seen = new Set(entries.map((e) => e.id));
      const fresh = page.filter((e) => !seen.has(e.id));
      entries = [...entries, ...fresh];
      hasMore = page.length >= PAGE_SIZE;
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      loadingMore = false;
    }
  }

  // Initial load is owned by the $effect below (runs once on mount); a separate
  // onMount(refresh) double-fetched on first paint.
  $effect(() => {
    typeFilter; opFilter;     // dependencies
    hasMore = true;
    refresh();
  });

  // The `payload` shape varies per (resource_type, op). For updates
  // the canonical shape is `{before: {...}, after: {...}}` — render
  // those as per-field old→new pairs so operators can see the actual
  // change, not just which key moved. Falls back to JSON for shapes we
  // don't recognise (e.g. action: "password_reset", create payloads).
  function hasBeforeAfter(p: Record<string, unknown>): p is {
    before: Record<string, unknown>;
    after: Record<string, unknown>;
  } {
    return (
      typeof p.before === "object" && p.before !== null &&
      typeof p.after === "object" && p.after !== null
    );
  }

  function formatScalar(v: unknown): string {
    if (v === null || v === undefined) return "—";
    if (typeof v === "string") return v.length === 0 ? '""' : v;
    if (typeof v === "boolean") return v ? "true" : "false";
    if (typeof v === "number") return String(v);
    // Pretty-print nested structures (zone rules, polygon-vertex
    // summary). The cell uses whitespace-pre-wrap so multi-line is fine.
    try {
      return JSON.stringify(v, null, 2);
    } catch {
      return String(v);
    }
  }

  type DiffRow = { key: string; before: unknown; after: unknown };
  function diffRows(e: AuditEntry): DiffRow[] {
    if (!hasBeforeAfter(e.payload)) return [];
    const b = e.payload.before;
    const a = e.payload.after;
    const keys = new Set<string>([...Object.keys(b), ...Object.keys(a)]);
    return [...keys].sort().map((k) => ({ key: k, before: b[k], after: a[k] }));
  }

  function payloadSummary(e: AuditEntry): string {
    const p = e.payload as Record<string, unknown>;
    // Zone rows carry camera + zone identity in the payload so we can
    // render "Zone X on Camera Y" instead of just the resource type.
    if (e.resource_type === "zone") {
      const zone =
        (typeof p.zone_name === "string" && p.zone_name) ||
        (typeof p.name === "string" && p.name) || "";
      const camera =
        (typeof p.camera_name === "string" && p.camera_name) ||
        (typeof p.camera_slug === "string" && p.camera_slug) || "";
      if (zone && camera) {
        return t("audit_what_zone_on_camera")
          .replace("{zone}", zone)
          .replace("{camera}", camera);
      }
      if (zone) {
        return t("audit_what_zone_no_camera").replace("{zone}", zone);
      }
    }
    if (e.op === "create" || e.op === "delete") {
      if (typeof p.name === "string") return String(p.name);
      if (typeof p.username === "string") return String(p.username);
      if (typeof p.slug === "string") return String(p.slug);
    }
    if (e.op === "purge" && typeof p.deleted === "number") {
      return String(p.deleted);
    }
    if (e.op === "update") {
      if (hasBeforeAfter(p)) {
        return Object.keys(p.after).join(", ");
      }
      // Legacy shape — older rows still in the table.
      const fc = p.fields_changed;
      if (Array.isArray(fc)) return fc.join(", ");
      if (p.action === "password_reset") return "password reset";
    }
    return "";
  }

  async function runPurge() {
    purgeRunning = true;
    purgeResult = null;
    try {
      const res = await api.purgeAudit(purgeDays);
      const n = res.deleted;
      if (n === 0) {
        purgeResult = t("audit_purge_done_zero");
      } else {
        const key = plural(
          n,
          "audit_purge_done_one",
          "audit_purge_done_few",
          "audit_purge_done_many",
        );
        purgeResult = key.replace("{n}", String(n));
      }
      await refresh();
    } catch (e) {
      purgeResult = e instanceof Error ? e.message : String(e);
    } finally {
      purgeRunning = false;
    }
  }

  function changesLabel(n: number): string {
    return `${n} ${plural(
      n,
      "audit_changes_word_one",
      "audit_changes_word_few",
      "audit_changes_word_many",
    )}`;
  }
</script>

<div class="space-y-6">

  <!-- Filters row -->
  <Card>
    <div class="flex flex-wrap items-end gap-3">
      <label class="flex flex-col gap-1 text-s">
        <span class="text-baba-text-faint">{t("audit_filter_type")}</span>
        <select
          bind:value={typeFilter}
          class="rounded border border-baba-border bg-baba-bg px-2 py-1 text-m"
        >
          <option value="">{t("audit_filter_all")}</option>
          {#each byLabel(facets.resource_types, resourceLabel) as rt (rt)}
            <option value={rt}>{resourceLabel(rt)}</option>
          {/each}
        </select>
      </label>

      <label class="flex flex-col gap-1 text-s">
        <span class="text-baba-text-faint">{t("audit_filter_op")}</span>
        <select
          bind:value={opFilter}
          class="rounded border border-baba-border bg-baba-bg px-2 py-1 text-m"
        >
          <option value="">{t("audit_filter_all")}</option>
          {#each byLabel(facets.ops, opLabel) as o (o)}
            <option value={o}>{opLabel(o)}</option>
          {/each}
        </select>
      </label>

      {#if auth.user?.role === "admin"}
        <div class="ml-auto">
          <Button onclick={() => { purgeOpen = true; purgeResult = null; }}>{t("audit_purge_button")}</Button>
        </div>
      {/if}
    </div>
  </Card>

  {#if purgeOpen}
    <Dialog title={t("audit_purge_title")} size="narrow" onclose={() => { if (!purgeRunning) purgeOpen = false; }}>
        <p class="text-s text-baba-text-muted">{t("audit_purge_desc")}</p>

        <fieldset class="mt-3">
          <legend class="text-s text-baba-text-faint">{t("audit_purge_choose")}</legend>
          <div class="mt-1 flex flex-col gap-1">
            {#each PURGE_WINDOWS as w}
              <label class="flex items-center gap-2 text-m">
                <input
                  type="radio"
                  name="purge-window"
                  value={w.days}
                  bind:group={purgeDays}
                  disabled={purgeRunning}
                />
                {t(w.key)}
              </label>
            {/each}
          </div>
        </fieldset>

        {#if purgeResult}
          <p class="mt-3 text-m text-baba-text-muted">{purgeResult}</p>
        {/if}

        <div class="mt-4 flex justify-end gap-2">
          <Button onclick={() => { purgeOpen = false; }} disabled={purgeRunning}>{t("audit_purge_cancel")}</Button>
          <Button tone="danger" onclick={runPurge} disabled={purgeRunning}>{purgeRunning ? t("audit_purge_running") : t("audit_purge_confirm")}</Button>
        </div>
    </Dialog>
  {/if}

  {#if loading}
    <p class="text-m text-baba-text-muted">{t("audit_loading")}</p>
  {:else if error}
    <p class="text-m text-red-400">{t("cameras_error_prefix")}: {error}</p>
  {:else if entries.length === 0}
    <p class="text-m text-baba-text-faint">{t("audit_empty")}</p>
  {:else}
    <div class="overflow-x-auto rounded-lg border border-baba-border bg-baba-panel">
      <table class="w-full text-m">
        <thead class="bg-baba-panel-2 text-s uppercase tracking-wide text-baba-text-faint">
          <tr>
            <th class="px-3 py-2 text-left">{t("audit_col_when")}</th>
            <th class="px-3 py-2 text-left">{t("audit_col_who")}</th>
            <th class="px-3 py-2 text-left">{t("audit_col_what")}</th>
            <th class="px-3 py-2 text-left">{t("audit_col_op")}</th>
            <th class="px-3 py-2 text-left">{t("audit_col_payload")}</th>
          </tr>
        </thead>
        <tbody class="divide-y divide-baba-border">
          {#each entries as e (e.id)}
            {@const isOpen = expanded[e.id] ?? false}
            {@const rows = diffRows(e)}
            <tr
              class="cursor-pointer hover:bg-baba-panel-2"
              onclick={() => (expanded = { ...expanded, [e.id]: !isOpen })}
            >
              <td class="whitespace-nowrap px-3 py-2 text-s text-baba-text-muted" title={dt.full(e.at)}>
                {dt.short(e.at)}
              </td>
              <td class="px-3 py-2">
                {#if e.username}
                  {e.username}
                {:else}
                  <span class="text-baba-text-faint italic">{t("audit_user_system")}</span>
                {/if}
              </td>
              <td class="px-3 py-2">
                {#if e.resource_type === "zone" && payloadSummary(e)}
                  <!-- Zone summary already contains the camera + zone name; printing
                       the bare "Zone" type in front would just duplicate it. -->
                  <span>{payloadSummary(e)}</span>
                {:else}
                  <span class="text-baba-text-muted">{resourceLabel(e.resource_type)}</span>
                  {#if payloadSummary(e)}
                    <span class="ml-1">{payloadSummary(e)}</span>
                  {/if}
                {/if}
              </td>
              <td class="px-3 py-2">
                <Tag tone={OP_TONE[e.op] ?? "warn"}>{opLabel(e.op)}</Tag>
              </td>
              <td class="px-3 py-2 text-s text-baba-text-faint">
                {isOpen ? "▾" : "▸"}
                {#if rows.length > 0}
                  {changesLabel(rows.length)}
                {:else}
                  {Object.keys(e.payload).length} {t("audit_changes_keys")}
                {/if}
              </td>
            </tr>
            {#if isOpen}
              <tr class="bg-baba-bg">
                <td colspan="5" class="px-3 py-2">
                  {#if rows.length > 0}
                    <div class="overflow-x-auto rounded border border-baba-border bg-baba-panel-2">
                      <table class="w-full text-s">
                        <thead class="text-baba-text-faint">
                          <tr>
                            <th class="px-2 py-1 text-left font-medium">{t("audit_changes_field")}</th>
                            <th class="px-2 py-1 text-left font-medium">{t("audit_changes_before")}</th>
                            <th class="px-2 py-1 text-left font-medium">{t("audit_changes_after")}</th>
                          </tr>
                        </thead>
                        <tbody class="divide-y divide-baba-border align-top">
                          {#each rows as r (r.key)}
                            <tr>
                              <td class="whitespace-nowrap px-2 py-1 font-mono">{r.key}</td>
                              <td class="px-2 py-1 font-mono text-red-300/90 whitespace-pre-wrap break-all">{formatScalar(r.before)}</td>
                              <td class="px-2 py-1 font-mono text-emerald-300/90 whitespace-pre-wrap break-all">{formatScalar(r.after)}</td>
                            </tr>
                          {/each}
                        </tbody>
                      </table>
                    </div>
                  {:else if Object.keys(e.payload).length === 0}
                    <p class="text-s text-baba-text-faint italic">{t("audit_changes_no_diff")}</p>
                  {:else}
                    <pre class="overflow-x-auto whitespace-pre-wrap rounded border border-baba-border bg-baba-panel-2 p-2 font-mono text-s">{JSON.stringify(e.payload, null, 2)}</pre>
                  {/if}
                  {#if e.resource_id}
                    <div class="mt-1 text-s text-baba-text-faint">
                      resource_id: <span class="font-mono">{e.resource_id}</span>
                    </div>
                  {/if}
                </td>
              </tr>
            {/if}
          {/each}
        </tbody>
      </table>
    </div>

    <div class="mt-3 text-center">
      {#if hasMore}
        <Button onclick={loadMore} disabled={loadingMore}>
          {loadingMore ? t("events_loading_more") : t("audit_load_more")}
        </Button>
      {:else}
        <p class="text-s text-baba-text-faint">{t("audit_end_of_list")}</p>
      {/if}
    </div>
  {/if}
</div>
