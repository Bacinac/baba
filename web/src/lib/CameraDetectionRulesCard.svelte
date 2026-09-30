<script lang="ts">
  import { byLabel } from "$lib/order";
  import { formatNumber, Button, Card, Tag, Toggle } from "$lib/kit";
  import { t } from "$lib/i18n";
  import { onMount } from "svelte";
  import {
    api,
    type CameraDetectionRule,
    type EffectiveDetectionRule,
    type GlobalDetectionRule,
  } from "$lib/api";
  import { classLabel } from "$lib/classLabels";

  type Props = { cameraId: string };
  let { cameraId }: Props = $props();

  let globalRules = $state<GlobalDetectionRule[]>([]);
  let overrides = $state<CameraDetectionRule[]>([]);
  let loading = $state(true);
  let error = $state<string | null>(null);
  let savingClass = $state<string | null>(null);
  let pickerOpen = $state(false);
  let pickerValue = $state("");
  // Confidence values mid-drag, keyed by class. The committed value lives in
  // `overrides`/`globalRules`; this overlay keeps the slider smooth between
  // `oninput` ticks without writing a partial override on every pixel.
  let drag = $state<Record<string, number>>({});
  // Live-drag buffer for the min-size slider — same pattern as `drag` for
  // confidence: oninput updates locally, onchange persists.
  let dragSize = $state<Record<string, number>>({});

  async function refresh() {
    loading = true;
    error = null;
    try {
      const [g, ov, eff] = await Promise.all([
        api.listGlobalDetectionRules(),
        api.listCameraDetectionRules(cameraId),
        api.listCameraDetectionRulesEffective(cameraId),
      ]);
      globalRules = g;
      overrides = ov;
      effective = eff;
    } catch (e) {
      error = (e as Error).message;
    } finally {
      loading = false;
    }
  }

  onMount(refresh);

  // The effective detection set for THIS camera = every globally-enabled
  // class (the baseline that runs on all cameras) UNION every class with a
  // per-camera override (which may enable a globally-off class, or disable /
  // retune an enabled one). Showing the full set — not just the overrides —
  // is the point: zones never gate WHICH classes are detected, so without
  // this list the operator can't see what an un-zoned camera actually
  // watches for.
  // Resolved server-side by the detector's own code (baba_core.rule_resolve).
  // Deriving it here is what produced the bug this replaces: a class with no
  // global row fell back to a made-up 0.25 and rendered as enabled, while the
  // detector was dropping it outright and no per-camera override could
  // rescue it.
  let effective = $state<EffectiveDetectionRule[]>([]);

  type EffRow = {
    cls: string;
    global: GlobalDetectionRule | null;
    override: CameraDetectionRule | null;
    enabled: boolean;     // effective on this camera
    confidence: number;   // effective on this camera
    minBoxPct: number;    // effective on this camera
    overridden: boolean;  // a per-camera row actually changes the outcome
    source: string;       // unconfigured | global | override | not-allowed
  };

  let rows = $derived.by<EffRow[]>(() => {
    const names = new Set<string>();
    for (const g of globalRules) if (g.enabled) names.add(g.class_name);
    for (const o of overrides) names.add(o.class_name);
    return byLabel([...names], classLabel)
      .map((cls) => {
        const g = globalRules.find((x) => x.class_name === cls) ?? null;
        const o = overrides.find((x) => x.class_name === cls) ?? null;
        const e = effective.find((x) => x.class_name === cls) ?? null;
        // No resolved row can only mean the lists and the resolution
        // disagree; show it as dropped rather than inventing a number.
        const enabled = e?.enabled ?? false;
        const confidence = e?.min_confidence ?? 1;
        const minBoxPct = e?.min_box_pct ?? 0;
        const source = e?.source ?? "not-allowed";
        return {
          cls, global: g, override: o, enabled, confidence, minBoxPct,
          overridden: source === "override", source,
        };
      });
  });

  // Picker offers classes in the global allowlist that aren't already shown
  // as a row — i.e. globally-disabled classes you want to switch on for just
  // this camera. (Globally-enabled classes are always rows already.)
  let pickable = $derived(
    byLabel(
      globalRules.map((g) => g.class_name).filter((n) => !rows.some((r) => r.cls === n)),
      classLabel,
    ),
  );

  async function saveOverride(
    cls: string, patch: Partial<CameraDetectionRule>,
  ) {
    savingClass = cls;
    try {
      const existing = overrides.find((o) => o.class_name === cls);
      const g = globalRules.find((x) => x.class_name === cls) ?? null;
      const next = {
        min_confidence: patch.min_confidence !== undefined
          ? patch.min_confidence
          : existing?.min_confidence ?? null,
        enabled: patch.enabled !== undefined
          ? patch.enabled
          : existing?.enabled ?? null,
        min_box_pct: patch.min_box_pct !== undefined
          ? patch.min_box_pct
          : existing?.min_box_pct ?? null,
      };
      // Don't persist a row that changes nothing. Two ways that happens:
      // it resolves to the global default, or there IS no global row — in
      // which case the class is dropped outright and no per-camera value can
      // rescue it, so storing one would only look like it did something.
      const effConf = next.min_confidence ?? g?.min_confidence ?? 0;
      const effEnabled = next.enabled ?? g?.enabled ?? false;
      const effMinBox = next.min_box_pct ?? g?.min_box_pct ?? 0;
      const gConf = g?.min_confidence ?? 0;
      const gEnabled = g?.enabled ?? false;
      const gMinBox = g?.min_box_pct ?? 0;
      const inert =
        g == null ||
        (effEnabled === gEnabled &&
          Math.abs(effConf - gConf) < 0.005 &&
          Math.abs(effMinBox - gMinBox) < 0.05);
      if (inert) {
        if (existing) {
          await api.deleteCameraDetectionRule(cameraId, cls);
          overrides = overrides.filter((o) => o.class_name !== cls);
          await refresh();
        }
        return;
      }
      const saved = await api.putCameraDetectionRule(cameraId, cls, next);
      overrides = existing
        ? overrides.map((o) => (o.class_name === cls ? saved : o))
        : [...overrides, saved];
      // The rows render from `effective`, which the server computes and which
      // was fetched once on mount. Updating only `overrides` left every row
      // showing the values from before the write, so a saved change appeared
      // not to have taken until the page was reloaded.
      await refresh();
    } catch (e) {
      error = (e as Error).message;
    } finally {
      savingClass = null;
      delete drag[cls];
      delete dragSize[cls];
    }
  }

  // Revert to the global default by dropping the per-camera override.
  async function revert(cls: string) {
    savingClass = cls;
    try {
      await api.deleteCameraDetectionRule(cameraId, cls);
      overrides = overrides.filter((o) => o.class_name !== cls);
    } catch (e) {
      error = (e as Error).message;
    } finally {
      savingClass = null;
      delete drag[cls];
      delete dragSize[cls];
    }
  }

  async function addClass() {
    if (!pickerValue) return;
    const cls = pickerValue;
    pickerValue = "";
    pickerOpen = false;
    // Enable a globally-off class for just this camera (inherit confidence).
    await saveOverride(cls, { min_confidence: null, enabled: true });
  }
</script>

<Card title={t("detection_rules_camera_title")}>
  <p class="text-m text-baba-text-faint">{t("detection_rules_camera_desc")}</p>

  {#if loading}
    <p class="text-baba-text-faint">{t("cameras_loading")}</p>
  {:else if error}
    <p class="text-red-400">{error}</p>
  {:else if rows.length === 0}
    <p class="text-s text-baba-text-faint italic">
      {t("detection_rules_camera_none")}
    </p>
  {:else}
    <div class="space-y-2">
      <div
        class="grid grid-cols-[11rem_1fr_3.5rem_1fr_4.5rem_5rem_auto] items-center gap-3 px-3 text-2xs font-medium uppercase tracking-wide text-baba-text-faint"
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
      {#each rows as r (r.cls)}
        <div
          class="grid grid-cols-[11rem_1fr_3.5rem_1fr_4.5rem_5rem_auto] items-center gap-3 rounded border bg-baba-bg/40 px-3 py-2"
          class:border-baba-accent={r.overridden}
          class:border-baba-border={!r.overridden}
        >
          <div class="flex items-center gap-2 text-m font-medium">
            <span class="truncate">{r.cls}</span>
            {#if r.overridden}
              <Tag tone="busy">
                {t("detection_rules_source_camera")}
              </Tag>
            {:else}
              <Tag tone="quiet">
                {t("detection_rules_source_global")}
              </Tag>
            {/if}
          </div>
          <input
            type="range"
            min="0" max="1" step="0.05"
            value={drag[r.cls] ?? r.confidence}
            disabled={!r.enabled || savingClass === r.cls}
            oninput={(e) => {
              drag[r.cls] = parseFloat((e.target as HTMLInputElement).value);
            }}
            onchange={(e) => saveOverride(r.cls, {
              min_confidence: parseFloat((e.target as HTMLInputElement).value),
            })}
            class="w-full"
            class:opacity-40={!r.enabled}
          />
          <div class="text-right font-mono text-s text-baba-text-faint">
            {formatNumber(drag[r.cls] ?? r.confidence, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
          </div>
          <input
            type="range"
            min="0" max="50" step="0.5"
            value={dragSize[r.cls] ?? r.minBoxPct}
            disabled={!r.enabled || savingClass === r.cls}
            title={t("detection_rules_min_size_help")}
            oninput={(e) => {
              dragSize[r.cls] = parseFloat((e.target as HTMLInputElement).value);
            }}
            onchange={(e) => saveOverride(r.cls, {
              min_box_pct: parseFloat((e.target as HTMLInputElement).value),
            })}
            class="w-full accent-sky-500"
            class:opacity-40={!r.enabled}
          />
          <div
            class="text-right font-mono text-s text-baba-text-faint"
            title={t("detection_rules_min_size")}
          >
            {formatNumber(dragSize[r.cls] ?? r.minBoxPct, { minimumFractionDigits: 1, maximumFractionDigits: 1 })}%
          </div>
          <label class="inline-flex items-center gap-2 text-s">
            <Toggle size="small" checked={r.enabled} disabled={savingClass === r.cls} onclick={() => saveOverride(r.cls, { enabled: !r.enabled })} />
            {t("detection_rules_enabled")}
          </label>
          {#if r.overridden}
            <Button size="small" onclick={() => revert(r.cls)} disabled={savingClass === r.cls} title={t("detection_rules_revert")} label={t("detection_rules_revert")}>↺</Button>
          {:else}
            <span class="w-4" aria-hidden="true"></span>
          {/if}
        </div>
      {/each}
    </div>

    {#if pickable.length > 0}
      <div class="mt-4 flex items-center gap-2">
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
            {#each pickable as cls (cls)}
              <option value={cls}>{classLabel(cls)}</option>
            {/each}
          </select>
          <Button tone="primary" onclick={addClass} disabled={!pickerValue}>
            {t("detection_rules_add")}
          </Button>
          <Button size="small" label={t("common.close")} onclick={() => { pickerOpen = false; pickerValue = ""; }}>✕</Button>
        {/if}
      </div>
    {/if}
  {/if}
</Card>
