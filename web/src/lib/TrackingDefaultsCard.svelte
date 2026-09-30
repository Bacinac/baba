<script lang="ts">
  import { Card } from "$lib/kit";
  // Global tracking defaults (app_settings.tracking_defaults) — the base
  // layer every camera inherits unless it carries a per-camera override.
  // Sliders save the WHOLE object on release (PUT), same as the telemetry
  // settings switch; the tracker hot-reloads via app_settings_changed.
  import { onMount } from "svelte";
  import { api, type TrackingDefaults } from "$lib/api";
  import { t, type MessageKey } from "$lib/i18n";
  let defaults = $state<TrackingDefaults | null>(null);
  let drag = $state<Partial<TrackingDefaults>>({});
  let saving = $state(false);
  let error = $state<string | null>(null);

  onMount(async () => {
    try {
      defaults = await api.getTrackingDefaults();
    } catch (e) {
      error = (e as Error).message;
    }
  });

  async function save(patch: Partial<TrackingDefaults>) {
    if (!defaults) return;
    saving = true;
    try {
      defaults = await api.putTrackingDefaults({ ...defaults, ...patch });
      error = null;
    } catch (e) {
      error = (e as Error).message;
    } finally {
      saving = false;
      drag = {};
    }
  }

  const rows: {
    key: keyof TrackingDefaults;
    label: MessageKey;
    hint: MessageKey;
    min: number; max: number; step: number;
    fmt: (v: number) => string;
    parse: (s: string) => number;
  }[] = [
    {
      key: "stillness_ratio",
      label: "camera_stillness_ratio",
      hint: "camera_stillness_ratio_hint",
      min: 0.05, max: 0.3, step: 0.01,
      fmt: (v: number) => `${Math.round(v * 100)}%`,
      parse: (s: string) => parseFloat(s),
    },
    {
      key: "park_seconds",
      label: "camera_park_seconds",
      hint: "camera_park_seconds_hint",
      min: 15, max: 300, step: 5,
      fmt: (v: number) => `${v}s`,
      parse: (s: string) => parseInt(s, 10),
    },
    {
      key: "lost_seconds",
      label: "camera_lost_seconds",
      hint: "camera_lost_seconds_hint",
      min: 5, max: 120, step: 5,
      fmt: (v: number) => `${v}s`,
      parse: (s: string) => parseInt(s, 10),
    },
    {
      key: "reid_lost_seconds",
      label: "camera_reid_lost_seconds",
      hint: "camera_reid_lost_seconds_hint",
      min: 0, max: 300, step: 10,
      fmt: (v: number) => `${v}s`,
      parse: (s: string) => parseInt(s, 10),
    },
  ];
</script>

<Card title={t("tracking_global_title")}>
  <p class="mt-1 text-m text-baba-text-muted">{t("tracking_global_desc")}</p>
  {#if error}
    <p class="mt-2 text-m text-red-400">{error}</p>
  {/if}
  {#if defaults}
    <div class="mt-3 grid grid-cols-1 gap-4 md:grid-cols-2">
      {#each rows as r (r.key)}
        <label class="block">
          <span class="block text-s text-baba-text-muted">{t(r.label)}</span>
          <div class="mt-1 flex items-center gap-3">
            <input
              type="range"
              min={r.min} max={r.max} step={r.step}
              value={drag[r.key] ?? defaults[r.key]}
              disabled={saving}
              oninput={(e) => (drag = { ...drag, [r.key]: r.parse((e.target as HTMLInputElement).value) })}
              onchange={(e) => save({ [r.key]: r.parse((e.target as HTMLInputElement).value) })}
              class="w-full accent-sky-500"
            />
            <span class="w-12 text-right font-mono text-s text-baba-text-faint">
              {r.fmt(drag[r.key] ?? defaults[r.key])}
            </span>
          </div>
          <p class="mt-1 text-xs text-baba-text-faint">{t(r.hint)}</p>
        </label>
      {/each}
    </div>
  {/if}
</Card>
