<script lang="ts">
  import { byLabel } from "$lib/order";
  import { formatNumber, Button, Card } from "$lib/kit";
  import { t } from "$lib/i18n";
  import {
    api,
    type Zone,
    type ZoneClassRule,
    type ZoneRules,
    type GlobalDetectionRule,
  } from "$lib/api";
  import { untrack } from "svelte";
  import { classLabel } from "$lib/classLabels";

  type Props = {
    zone: Zone;
    onUpdate: (z: Zone) => void;
    // Fires on every slider tick before the rule is committed to the
    // server. The page uses this to preview the would-be filter on the
    // live detection overlay so the operator can dial in a threshold
    // visually before releasing the slider.
    onPreview?: (z: Zone) => void;
  };
  let { zone: zoneProp, onUpdate, onPreview }: Props = $props();
  // Local, optimistically-updatable copy: rule tweaks reflect instantly in the
  // card before the server round-trips. Synced from the prop so selecting a
  // different zone resets it — instead of reassigning the $props() value
  // directly, which trips Svelte 5's ownership warning and gets clobbered on
  // the parent's next render. untrack() states that the seed is deliberately
  // a one-shot read; the $effect below owns the resync.
  let zone = $state(untrack(() => zoneProp));
  $effect(() => { zone = zoneProp; });

  let saving = $state(false);
  let error = $state<string | null>(null);
  let pickerOpen = $state(false);
  let pickerValue = $state("");
  let globalClasses = $state<string[]>([]);

  // Load the set of enabled global classes once — the picker offers
  // only classes that the global allowlist already permits, since a
  // zone rule for a globally-disabled class would never fire.
  $effect(() => {
    api.listGlobalDetectionRules().then((rs) => {
      globalClasses = rs
        .filter((r: GlobalDetectionRule) => r.enabled)
        .map((r) => r.class_name);
    }).catch(() => {});
  });

  // Local copy of the rules so the inputs stay snappy; flushed to the
  // server on commit (onchange / blur).
  let enabledClasses = $derived(zone.rules?.enabled_classes ?? {});
  let entries = $derived(
    byLabel(Object.entries(enabledClasses), ([cls]) => classLabel(cls))
  );
  let pickable = $derived(
    byLabel(globalClasses.filter((c) => !(c in enabledClasses)), classLabel),
  );

  async function commitRules(next: ZoneRules) {
    saving = true;
    error = null;
    try {
      const updated = await api.patchZone(zone.id, { rules: next });
      onUpdate(updated);
    } catch (e) {
      error = (e as Error).message;
    } finally {
      saving = false;
    }
  }

  async function setClassRule(cls: string, patch: Partial<ZoneClassRule>) {
    const current = enabledClasses[cls] ?? {};
    const merged = { ...current, ...patch };
    const nextRules: ZoneRules = {
      enabled_classes: { ...enabledClasses, [cls]: merged },
    };
    await commitRules(nextRules);
  }

  async function removeClass(cls: string) {
    const next = { ...enabledClasses };
    delete next[cls];
    await commitRules({ enabled_classes: next });
  }

  async function addClass() {
    if (!pickerValue) return;
    const cls = pickerValue;
    pickerValue = "";
    pickerOpen = false;
    // Sensible defaults so the operator sees a working rule immediately
    // instead of an all-empty row.  Tuned for residential CCTV:
    //   - 0.50 confidence: comfortably above any global floor, kills
    //     low-conf single-frame hits without losing real targets
    //   - 5 s dwell:       short enough for "person stopped at door",
    //     long enough to filter walk-throughs
    //   - 30 s cooldown:   suppresses repeat events from the same zone
    //     when several people enter back-to-back
    //   - 0.5 % bbox area: rejects far-away pixel-noise detections
    //     that survived global confidence but aren't actionable
    await setClassRule(cls, {
      min_confidence: 0.5,
      min_dwell_ms: 5000,
      cooldown_s: 30,
      min_area_pct: 0.005,
    });
  }

  function nz(v: number | null | undefined): string {
    return v === null || v === undefined ? "" : String(v);
  }
</script>

<Card title={t("detection_rules_zone_title")}>
  <p class="text-s text-baba-text-faint">{t("detection_rules_zone_desc")}</p>

  {#if error}
    <p class="mb-2 text-s text-red-400">{error}</p>
  {/if}

  {#if entries.length === 0}
    <p class="text-s italic text-baba-text-faint">
      {t("detection_rules_zone_no_restriction")}
    </p>
  {:else}
    <div class="space-y-2">
      {#each entries as [cls, rule] (cls)}
        <!-- Compact vertical block per class — the aside is narrow so a
             7-column grid would overflow.  Header row carries the class
             name + remove button; below it sits the confidence slider;
             then a 3-col mini-grid with the optional dwell/cooldown/area
             fields, each labelled inline. -->
        <div class="rounded border border-baba-border bg-baba-bg/40 px-2 py-2">
          <div class="mb-1.5 flex items-center justify-between">
            <span class="truncate text-m font-medium" title={cls}>{classLabel(cls)}</span>
            <Button tone="danger" size="small" onclick={() => removeClass(cls)} disabled={saving} title={t("detection_rules_remove")} label={t("detection_rules_remove")}>✕</Button>
          </div>

          <div class="mb-2 flex items-center gap-2">
            <input
              type="range" min="0" max="1" step="0.05"
              value={rule.min_confidence ?? 0}
              oninput={(e) => {
                const v = parseFloat((e.target as HTMLInputElement).value);
                const nextZone = { ...zone, rules: {
                  enabled_classes: { ...enabledClasses, [cls]: { ...rule, min_confidence: v } },
                } } as Zone;
                zone = nextZone;
                onPreview?.(nextZone);
              }}
              onchange={(e) => setClassRule(cls, {
                min_confidence: parseFloat((e.target as HTMLInputElement).value),
              })}
              class="flex-1"
              aria-label={t("detection_rules_min_conf")}
            />
            <span class="w-9 text-right font-mono text-2xs text-baba-text-faint">
              {formatNumber(rule.min_confidence ?? 0, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
            </span>
          </div>

          <!-- 3-col grid: labels (one fixed-height row) + inputs (one
               aligned row).  Keeping labels and inputs in two separate
               grids lets us pin all three input boxes to the same
               baseline even when a label wraps to two lines. -->
          <div class="grid grid-cols-3 gap-1.5">
            <span class="text-2xs uppercase leading-tight tracking-wide text-baba-text-faint">
              {t("detection_rules_zone_dwell_ms")}
            </span>
            <span class="text-2xs uppercase leading-tight tracking-wide text-baba-text-faint">
              {t("detection_rules_zone_cooldown_s")}
            </span>
            <span class="text-2xs uppercase leading-tight tracking-wide text-baba-text-faint">
              {t("detection_rules_zone_min_area")}
            </span>

            <input
              type="number" min="0" step="100"
              value={nz(rule.min_dwell_ms)}
              placeholder="—"
              aria-label={t("detection_rules_zone_dwell_ms")}
              oninput={(e) => {
                const raw = (e.target as HTMLInputElement).value;
                const v = raw ? parseInt(raw, 10) : null;
                const nextZone = { ...zone, rules: {
                  enabled_classes: { ...enabledClasses, [cls]: { ...rule, min_dwell_ms: v } },
                } } as Zone;
                zone = nextZone;
                onPreview?.(nextZone);
              }}
              onchange={(e) => setClassRule(cls, {
                min_dwell_ms: (e.target as HTMLInputElement).value
                  ? parseInt((e.target as HTMLInputElement).value, 10) : null,
              })}
              class="w-full rounded border border-baba-border bg-baba-panel-2 px-1 py-1 text-s"
            />
            <input
              type="number" min="0" step="1"
              value={nz(rule.cooldown_s)}
              placeholder="—"
              aria-label={t("detection_rules_zone_cooldown_s")}
              onchange={(e) => setClassRule(cls, {
                cooldown_s: (e.target as HTMLInputElement).value
                  ? parseInt((e.target as HTMLInputElement).value, 10) : null,
              })}
              class="w-full rounded border border-baba-border bg-baba-panel-2 px-1 py-1 text-s"
            />
            <input
              type="number" min="0" max="100" step="0.5"
              value={rule.min_area_pct !== null && rule.min_area_pct !== undefined
                ? String(Math.round(rule.min_area_pct * 1000) / 10) : ""}
              placeholder="—"
              aria-label={t("detection_rules_zone_min_area")}
              oninput={(e) => {
                const raw = (e.target as HTMLInputElement).value;
                const v = raw ? parseFloat(raw) / 100 : null;
                const nextZone = { ...zone, rules: {
                  enabled_classes: { ...enabledClasses, [cls]: { ...rule, min_area_pct: v } },
                } } as Zone;
                zone = nextZone;
                onPreview?.(nextZone);
              }}
              onchange={(e) => {
                const raw = (e.target as HTMLInputElement).value;
                setClassRule(cls, {
                  min_area_pct: raw ? parseFloat(raw) / 100 : null,
                });
              }}
              class="w-full rounded border border-baba-border bg-baba-panel-2 px-1 py-1 text-s"
            />
          </div>
        </div>
      {/each}
    </div>
  {/if}

  {#if pickable.length > 0}
    <div class="mt-3 flex items-center gap-2">
      {#if !pickerOpen}
        <Button size="small" onclick={() => { pickerOpen = true; pickerValue = ""; }} disabled={saving}>+ {t("detection_rules_add")}</Button>
      {:else}
        <select
          class="rounded border border-baba-border bg-baba-bg px-2 py-1 text-s"
          bind:value={pickerValue}
        >
          <option value="">{t("detection_rules_pick_class")}</option>
          {#each pickable as cls (cls)}
            <option value={cls}>{classLabel(cls)}</option>
          {/each}
        </select>
        <Button tone="primary" size="small" onclick={addClass} disabled={!pickerValue || saving}>{t("detection_rules_add")}</Button>
        <Button size="small" label={t("common.close")} onclick={() => { pickerOpen = false; pickerValue = ""; }}>✕</Button>
      {/if}
      {#if saving}
        <span class="text-s text-baba-text-faint italic">{t("detection_rules_saving")}</span>
      {/if}
    </div>
  {/if}
</Card>
