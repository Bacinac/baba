<script lang="ts">
  // Pipeline-incidents panel (System page). The event-manager's watchers
  // open incidents over the telemetry flight recorder; here the operator
  // reads the AI verdict (or runs it manually), applies its constrained
  // suggestion or dismisses. Compact table — one row per incident, click
  // to expand the verdict/actions; auto-analyze switch in the header.
  import { onMount } from "svelte";
  import {
    api,
    type TelemetryIncident,
    type IncidentChange,
    type TelemetryUsage,
  } from "$lib/api";
  import { formatNumber, Button, Tag, type TagTone, Toggle } from "$lib/kit";
  import { t, type MessageKey } from "$lib/i18n";
  import { dt } from "$lib/datetime.svelte";
  import IncidentReplay from "$lib/IncidentReplay.svelte";

  let incidents = $state<TelemetryIncident[]>([]);
  let error = $state<string | null>(null);
  let busy = $state<Record<string, "analyze" | "apply" | "dismiss" | "revert">>({});
  let showHistory = $state(false);
  let expanded = $state<string | null>(null);
  let replaying = $state<string | null>(null);
  let autoAnalyze = $state(false);
  let autoSaving = $state(false);
  let usage = $state<TelemetryUsage | null>(null);

  const active = $derived(incidents.filter((i) => i.status === "open" || i.status === "analyzed"));
  const history = $derived(incidents.filter((i) => i.status !== "open" && i.status !== "analyzed"));
  const shown = $derived(showHistory ? [...active, ...history] : active);

  async function load() {
    try {
      incidents = await api.listIncidents(true);
      error = null;
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    }
    api.getTelemetryUsage().then((u) => (usage = u)).catch(() => {});
  }

  onMount(() => {
    load();
    api.getTelemetrySettings().then((s) => (autoAnalyze = s.auto_analyze)).catch(() => {});
    const id = setInterval(load, 60_000);
    return () => clearInterval(id);
  });

  async function toggleAuto() {
    autoSaving = true;
    try {
      const s = await api.putTelemetrySettings({ auto_analyze: !autoAnalyze });
      autoAnalyze = s.auto_analyze;
      error = null;
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      autoSaving = false;
    }
  }

  function replace(updated: TelemetryIncident) {
    incidents = incidents.map((i) => (i.id === updated.id ? updated : i));
  }

  async function analyze(inc: TelemetryIncident) {
    busy = { ...busy, [inc.id]: "analyze" };
    try {
      const r = await api.analyzeIncident(inc.id);
      if (r.error) {
        error = r.error;
      } else if (r.incident) {
        replace(r.incident);
        error = null;
      }
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      const { [inc.id]: _, ...rest } = busy;
      busy = rest;
    }
  }

  async function apply(inc: TelemetryIncident) {
    busy = { ...busy, [inc.id]: "apply" };
    try {
      replace(await api.applyIncident(inc.id));
      error = null;
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      const { [inc.id]: _, ...rest } = busy;
      busy = rest;
    }
  }

  async function dismiss(inc: TelemetryIncident) {
    busy = { ...busy, [inc.id]: "dismiss" };
    try {
      replace(await api.dismissIncident(inc.id));
      error = null;
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      const { [inc.id]: _, ...rest } = busy;
      busy = rest;
    }
  }

  async function revert(inc: TelemetryIncident) {
    busy = { ...busy, [inc.id]: "revert" };
    try {
      replace(await api.revertIncident(inc.id));
      error = null;
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      const { [inc.id]: _, ...rest } = busy;
      busy = rest;
    }
  }

  const KIND_KEYS: Record<string, MessageKey> = {
    active_pinned: "incident_kind_active_pinned",
    drop_spike: "incident_kind_drop_spike",
  };
  const CAUSE_KEYS: Record<string, MessageKey> = {
    real_activity: "incident_cause_real_activity",
    false_positives: "incident_cause_false_positives",
    misconfigured_rule: "incident_cause_misconfigured_rule",
    model_instability: "incident_cause_model_instability",
    scene_change: "incident_cause_scene_change",
    unknown: "incident_cause_unknown",
  };

  function kindLabel(kind: string): string {
    const key = KIND_KEYS[kind];
    return key ? t(key) : kind;
  }

  function causeLabel(cause: string | undefined): string {
    if (!cause) return "";
    const key = CAUSE_KEYS[cause];
    return key ? t(key) : cause;
  }

  const STATUS_TONE: Record<string, TagTone> = {
    open: "warn",
    analyzed: "busy",
    applied: "ok",
    dismissed: "quiet",
    closed: "quiet",
    reverted: "fact",
  };

  // The 2-3 numbers that identify the incident at a glance; the full
  // evidence lives in the expanded row.
  const HEADLINE_KEYS: Record<string, string[]> = {
    active_pinned: ["duty", "max_active_tracks"],
    drop_spike: ["dropped", "published"],
  };

  // Some of these details are fractions: a duty cycle of 0.958 rounded to one
  // decimal reads "1", a saturated pipeline rather than a nearly-saturated one,
  // and 0.04 reads "0". A fraction keeps enough of itself to mean something.
  function metric(v: number): string {
    const a = Math.abs(v);
    return formatNumber(v, { maximumFractionDigits: a >= 100 ? 0 : a < 1 ? 3 : 2 });
  }

  function headline(inc: TelemetryIncident): string {
    const keys = HEADLINE_KEYS[inc.kind] ?? [];
    return keys
      .filter((k) => typeof inc.details[k] === "number")
      .map((k) => `${k.replace(/_/g, " ")} ${metric(inc.details[k] as number)}`)
      .join(" · ");
  }

  function evidencePairs(details: Record<string, unknown>): [string, string][] {
    return Object.entries(details)
      .filter(([k, v]) => k !== "window_s" && (typeof v === "number" || typeof v === "string"))
      .map(([k, v]) => [k, typeof v === "number" ? metric(v) : String(v)]);
  }

  function changes(inc: TelemetryIncident): IncidentChange[] {
    return inc.suggestion?.changes ?? [];
  }

  function changeTarget(c: IncidentChange): string {
    return c.target === "camera_rule" ? `${c.class_name} · ${c.field}` : c.field;
  }
</script>

<section class="rounded-lg border border-baba-border bg-baba-panel">
  <header class="flex flex-wrap items-center justify-between gap-2 border-b border-baba-border px-4 py-2">
    <h3 class="text-m font-medium">{t("system_section_incidents")}</h3>
    <div class="flex flex-wrap items-center gap-4">
      {#if usage && usage.analyses > 0}
        <span class="text-s tabular-nums text-baba-text-faint">
          {t("incidents_usage_prefix")}: {usage.analyses} ·
          {formatNumber(usage.input_tokens, { maximumFractionDigits: 1 })} / {formatNumber(usage.output_tokens, { maximumFractionDigits: 1 })}
          {t("incidents_usage_tokens")}
        </span>
      {/if}
      <label
        class="flex cursor-pointer items-center gap-2 text-s text-baba-text-muted"
        title={t("incidents_auto_analyze_hint")}
      >
        <Toggle size="small" checked={autoAnalyze} disabled={autoSaving} onclick={toggleAuto} />
        {t("incidents_auto_analyze")}
      </label>
    </div>
  </header>

  <div class="p-3">
    {#if error}
      <p class="mb-2 text-s text-red-400">{t("incidents_error_prefix")}: {error}</p>
    {/if}

    {#if shown.length === 0}
      <p class="text-m text-baba-text-muted">{t("incidents_empty")}</p>
    {:else}
      <div class="overflow-x-auto">
        <table class="w-full text-m">
          <thead>
            <tr class="text-left text-s text-baba-text-faint">
              <th class="px-2 py-1 font-normal">{t("incidents_col_camera")}</th>
              <th class="px-2 py-1 font-normal">{t("incidents_col_kind")}</th>
              <th class="px-2 py-1 font-normal">{t("incidents_col_evidence")}</th>
              <th class="px-2 py-1 font-normal">{t("incidents_col_status")}</th>
              <th class="px-2 py-1 text-right font-normal">{t("incidents_col_opened")}</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-baba-border">
            {#each shown as inc (inc.id)}
              {@const closed = inc.status !== "open" && inc.status !== "analyzed"}
              <tr
                class="cursor-pointer hover:bg-baba-panel-2 {closed ? 'opacity-50' : ''}"
                onclick={() => (expanded = expanded === inc.id ? null : inc.id)}
              >
                <td class="px-2 py-1.5 font-mono text-s">{inc.camera_slug}</td>
                <td class="px-2 py-1.5">{kindLabel(inc.kind)}</td>
                <td class="px-2 py-1.5 text-s tabular-nums text-baba-text-muted">{headline(inc)}</td>
                <td class="px-2 py-1.5">
                  <Tag tone={STATUS_TONE[inc.status] ?? "quiet"}>
                    {t(`incident_status_${inc.status}`)}
                  </Tag>
                  {#if inc.verdict}
                    <span class="ml-1 text-s text-baba-text-faint" title={inc.verdict}>💬</span>
                  {/if}
                </td>
                <td class="px-2 py-1.5 text-right text-s text-baba-text-faint">
                  {dt.short(inc.opened_at)}
                </td>
              </tr>

              {#if expanded === inc.id}
                <tr class={closed ? "opacity-70" : ""}>
                  <td colspan="5" class="bg-baba-panel-2/50 px-3 py-2">
                    <div class="flex flex-wrap gap-x-3 gap-y-1 text-s text-baba-text-muted">
                      <span class="text-baba-text-faint">{t("incident_evidence")}:</span>
                      {#each evidencePairs(inc.details) as [k, v] (k)}
                        <span><span class="text-baba-text-faint">{k}</span> {v}</span>
                      {/each}
                      {#if inc.closed_at}
                        <span>
                          <span class="text-baba-text-faint">{t("incident_closed_at")}</span>
                          {dt.short(inc.closed_at)}
                        </span>
                      {/if}
                    </div>

                    {#if inc.verdict}
                      <div class="mt-2 rounded border border-baba-border bg-baba-panel p-2 text-m">
                        <div class="flex flex-wrap items-baseline gap-2">
                          <span class="text-s font-medium text-baba-text-muted">
                            {t("incident_verdict")}
                          </span>
                          {#if inc.suggestion?.root_cause}
                            <Tag tone="quiet">
                              {causeLabel(inc.suggestion.root_cause)}
                            </Tag>
                          {/if}
                          {#if inc.suggestion?.model}
                            <span class="text-s tabular-nums text-baba-text-faint">
                              {inc.suggestion.model}{#if inc.suggestion.usage?.input_tokens}
                                · {formatNumber(inc.suggestion.usage.input_tokens, { maximumFractionDigits: 1 })} / {formatNumber(inc.suggestion.usage.output_tokens ?? 0, { maximumFractionDigits: 1 })} {t("incidents_usage_tokens")}{/if}
                            </span>
                          {/if}
                        </div>
                        <p class="mt-1">{inc.verdict}</p>
                        {#if changes(inc).length}
                          <div class="mt-2">
                            <span class="text-s font-medium text-baba-text-muted">
                              {t("incident_suggested_changes")}
                            </span>
                            <ul class="mt-1 space-y-1">
                              {#each changes(inc) as c, idx (idx)}
                                <li class="text-s">
                                  <span class="font-mono">{changeTarget(c)} → {metric(c.value)}</span>
                                  {#if c.reason}<span class="text-baba-text-muted"> — {c.reason}</span>{/if}
                                </li>
                              {/each}
                            </ul>
                          </div>
                        {:else}
                          <p class="mt-1 text-s text-baba-text-faint">{t("incident_no_changes")}</p>
                        {/if}
                      </div>
                    {/if}

                    {#if inc.status === "applied"}
                      <div class="mt-2">
                        <Button size="small" disabled={!!busy[inc.id]} onclick={(e) => {
                            e.stopPropagation();
                            revert(inc);
                          }}>
                          {busy[inc.id] === "revert"
                            ? t("incident_reverting")
                            : t("incident_revert")}
                        </Button>
                      </div>
                    {/if}
                    {#if !closed}
                      <div class="mt-2 flex flex-wrap gap-2">
                        <Button tone="accent" size="small" disabled={!!busy[inc.id]} onclick={(e) => { e.stopPropagation(); analyze(inc); }}>{busy[inc.id] === "analyze" ? t("incident_analyzing") : t("incident_analyze")}</Button>
                        {#if changes(inc).length}
                          <!-- Evidence before consent: the suggestion is an
                               argument until you have watched what it does to
                               the footage that produced it. -->
                          <Button size="small" selected={replaying === inc.id} onclick={(e) => { e.stopPropagation(); replaying = replaying === inc.id ? null : inc.id; }}>{t("incident_compare")}</Button>
                        {/if}
                        {#if inc.status === "analyzed" && changes(inc).length}
                          <Button tone="primary" size="small" disabled={!!busy[inc.id]} onclick={(e) => { e.stopPropagation(); apply(inc); }}>{busy[inc.id] === "apply" ? t("incident_applying") : t("incident_apply")}</Button>
                        {/if}
                        <Button size="small" disabled={!!busy[inc.id]} onclick={(e) => {
                            e.stopPropagation();
                            dismiss(inc);
                          }}>
                          {t("incident_dismiss")}
                        </Button>
                      </div>
                    {/if}

                    {#if replaying === inc.id}
                      <IncidentReplay incidentId={inc.id} onClose={() => (replaying = null)} />
                    {/if}
                  </td>
                </tr>
              {/if}
            {/each}
          </tbody>
        </table>
      </div>
    {/if}

    {#if history.length}
      <div class="mt-2"><Button size="small" onclick={() => (showHistory = !showHistory)}>{showHistory ? "▾" : "▸"} {t("incidents_show_history")} ({history.length})</Button></div>
    {/if}
  </div>
</section>
