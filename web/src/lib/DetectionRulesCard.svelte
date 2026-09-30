<script lang="ts">
  import { byLabel } from "$lib/order";
  import { formatNumber, Button, Card, Toggle } from "$lib/kit";
  import { t } from "$lib/i18n";
  import { onMount } from "svelte";
  import {
    api,
    type DetectionClass,
    type GlobalDetectionRule,
  } from "$lib/api";
  import { classLabel } from "$lib/classLabels";

  let classes = $state<DetectionClass[]>([]);
  let rules = $state<GlobalDetectionRule[]>([]);
  let loading = $state(true);
  let error = $state<string | null>(null);
  let savingClass = $state<string | null>(null);
  let pickerOpen = $state(false);
  let pickerValue = $state("");

  async function refresh() {
    loading = true;
    error = null;
    try {
      const [cls, rs] = await Promise.all([
        api.listDetectionClasses(),
        api.listGlobalDetectionRules(),
      ]);
      classes = cls;
      rules = rs;
    } catch (e) {
      error = (e as Error).message;
    } finally {
      loading = false;
    }
  }

  onMount(refresh);

  // Group rules by COCO group for display.  Lookup is by class name; an
  // unknown class (BYOM model with extra labels) falls into "other".
  function groupOf(name: string): string {
    return classes.find((c) => c.name === name)?.group ?? "other";
  }
  function groupLabel(g: string): string {
    if (g === "person") return t("detection_rules_group_person");
    if (g === "vehicle") return t("detection_rules_group_vehicle");
    if (g === "animal") return t("detection_rules_group_animal");
    return t("detection_rules_group_other");
  }
  let grouped = $derived.by(() => {
    const out: Record<string, GlobalDetectionRule[]> = {};
    for (const r of rules) {
      const g = groupOf(r.class_name);
      (out[g] ??= []).push(r);
    }
    // Stable order: person → vehicle → animal → other; alphabetical within.
    const order = ["person", "vehicle", "animal", "other"];
    return order
      .filter((g) => out[g]?.length)
      .map((g) => ({
        group: g,
        label: groupLabel(g),
        items: byLabel(out[g], (r) => classLabel(r.class_name)),
      }));
  });

  let unconfigured = $derived(
    byLabel(
      classes.filter((c) => !rules.some((r) => r.class_name === c.name)),
      (c) => classLabel(c.name),
    ),
  );

  async function save(rule: GlobalDetectionRule, patch: Partial<GlobalDetectionRule>) {
    const next = { ...rule, ...patch };
    savingClass = rule.class_name;
    try {
      const saved = await api.putGlobalDetectionRule(rule.class_name, {
        min_confidence: next.min_confidence,
        enabled: next.enabled,
        min_box_pct: next.min_box_pct ?? 0,
      });
      rules = rules.map((r) =>
        r.class_name === saved.class_name ? saved : r,
      );
    } catch (e) {
      error = (e as Error).message;
    } finally {
      savingClass = null;
    }
  }

  async function removeRule(rule: GlobalDetectionRule) {
    savingClass = rule.class_name;
    try {
      await api.deleteGlobalDetectionRule(rule.class_name);
      rules = rules.filter((r) => r.class_name !== rule.class_name);
    } catch (e) {
      error = (e as Error).message;
    } finally {
      savingClass = null;
    }
  }

  async function addClass() {
    if (!pickerValue) return;
    const name = pickerValue;
    pickerValue = "";
    pickerOpen = false;
    try {
      // Sensible defaults: 0.25 for person-like, 0.30 for vehicles,
      // 0.40 for animals, 0.35 for others.
      const g = classes.find((c) => c.name === name)?.group ?? "other";
      const defaultConf =
        g === "person" ? 0.25 :
        g === "vehicle" ? 0.30 :
        g === "animal" ? 0.40 : 0.35;
      const saved = await api.putGlobalDetectionRule(name, {
        min_confidence: defaultConf,
        enabled: true,
        min_box_pct: 0,
      });
      rules = [...rules, saved];
    } catch (e) {
      error = (e as Error).message;
    }
  }
</script>

<Card title={t("detection_rules_title")}>
  <p class="text-m text-baba-text-faint">{t("detection_rules_desc")}</p>

  {#if loading}
    <p class="text-baba-text-faint">{t("cameras_loading")}</p>
  {:else if error}
    <p class="text-red-400">{error}</p>
  {:else}
    <div class="space-y-5">
      <div
        class="grid grid-cols-[10rem_1fr_3.5rem_1fr_4.5rem_5rem_auto] items-center gap-3 px-3 text-2xs font-medium uppercase tracking-wide text-baba-text-faint"
        aria-hidden="true"
      >
        <span></span>
        <span class="flex items-center gap-1.5">
          <span class="inline-block h-1.5 w-1.5 rounded-full bg-red-500/80"></span>
          {t("detection_rules_min_conf")}
        </span>
        <span></span>
        <span class="flex items-center gap-1.5" title={t("detection_rules_min_size_help")}>
          <span class="inline-block h-1.5 w-1.5 rounded-full bg-sky-500/80"></span>
          {t("detection_rules_min_size")}
        </span>
        <span></span>
        <span></span>
        <span></span>
      </div>
      {#each grouped as section (section.group)}
        <div>
          <div class="mb-2 text-s font-semibold uppercase tracking-wide text-baba-text-faint">
            {section.label}
          </div>
          <div class="space-y-2">
            {#each section.items as r (r.class_name)}
              <div class="grid grid-cols-[10rem_1fr_3.5rem_1fr_4.5rem_5rem_auto] items-center gap-3 rounded border border-baba-border bg-baba-bg/40 px-3 py-2">
                <div class="text-m font-medium" class:opacity-50={!r.enabled}>
                  {classLabel(r.class_name)}
                </div>
                <input
                  type="range"
                  min="0" max="1" step="0.05"
                  value={r.min_confidence}
                  oninput={(e) => {
                    const v = parseFloat((e.target as HTMLInputElement).value);
                    rules = rules.map((x) =>
                      x.class_name === r.class_name
                        ? { ...x, min_confidence: v } : x,
                    );
                  }}
                  onchange={() => save(r, { min_confidence: r.min_confidence })}
                  class="w-full"
                />
                <div class="text-right font-mono text-s text-baba-text-faint">
                  {formatNumber(r.min_confidence, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                </div>
                <input
                  type="range"
                  min="0" max="50" step="0.5"
                  value={r.min_box_pct ?? 0}
                  title={t("detection_rules_min_size_help")}
                  oninput={(e) => {
                    const v = parseFloat((e.target as HTMLInputElement).value);
                    rules = rules.map((x) =>
                      x.class_name === r.class_name
                        ? { ...x, min_box_pct: v } : x,
                    );
                  }}
                  onchange={() => save(r, { min_box_pct: r.min_box_pct ?? 0 })}
                  class="w-full accent-sky-500"
                />
                <div
                  class="text-right font-mono text-s text-baba-text-faint"
                  title={t("detection_rules_min_size")}
                >
                  {formatNumber(r.min_box_pct ?? 0, { minimumFractionDigits: 1, maximumFractionDigits: 1 })}%
                </div>
                <label class="inline-flex items-center gap-2 text-s">
                  <Toggle size="small" checked={r.enabled} onclick={() => save(r, { enabled: !r.enabled })} />
                  {t("detection_rules_enabled")}
                </label>
                <Button tone="danger" size="small" onclick={() => removeRule(r)} disabled={savingClass === r.class_name} title={t("detection_rules_remove")} label={t("detection_rules_remove")}>✕</Button>
              </div>
            {/each}
          </div>
        </div>
      {/each}
    </div>

    <div class="mt-5 flex items-center gap-2">
      {#if !pickerOpen}
        <Button onclick={() => { pickerOpen = true; pickerValue = ""; }}>
          + {t("detection_rules_add")}
        </Button>
      {:else}
        <select
          class="rounded border border-baba-border bg-baba-bg px-2 py-1.5 text-m"
          bind:value={pickerValue}
        >
          <option value="">{t("detection_rules_pick_class")}</option>
          {#each unconfigured as c (c.name)}
            <option value={c.name}>{classLabel(c.name)} · {groupLabel(c.group)}</option>
          {/each}
        </select>
        <Button tone="primary" onclick={addClass} disabled={!pickerValue}>
          {t("detection_rules_add")}
        </Button>
        <Button size="small" label={t("common.close")} onclick={() => { pickerOpen = false; pickerValue = ""; }}>✕</Button>
      {/if}
    </div>
  {/if}
</Card>
