<script lang="ts">
  import { onMount } from "svelte";
  import { api, type UserPreferences } from "$lib/api";
  import { t } from "$lib/i18n";
  import { Card, Picks, Toggle, formatNumber } from "$lib/kit";
  import {
    playback,
    ACTIVITY_RANGES,
    CLIP_PREROLL_DEFAULT,
    CLIP_POSTROLL_DEFAULT,
    type ActivityRange,
  } from "$lib/playback.svelte";

  // Per-user viewing / playback preferences. Persisted in the same
  // /auth/preferences jsonb blob as theme/locale, applied app-wide via
  // auth.svelte `applyServerPrefs` → the playback store.
  const PREROLL_OPTIONS = [0, 2, 3, 5, 8, 12];
  const POSTROLL_OPTIONS = [0, 2, 4, 6, 10, 15];
  const seconds = (vs: number[]) => vs.map((v) => ({ key: String(v), label: `${formatNumber(v)} s` }));

  let preroll = $state(CLIP_PREROLL_DEFAULT);
  let postroll = $state(CLIP_POSTROLL_DEFAULT);
  let autoplay = $state(true);
  let range = $state<ActivityRange>("today");

  let loading = $state(true);
  let saved = $state(false);
  let error = $state<string | null>(null);
  let savedTimer: ReturnType<typeof setTimeout> | undefined;

  onMount(async () => {
    try {
      const p = await api.getPreferences();
      preroll = typeof p.clip_preroll_s === "number" ? p.clip_preroll_s : CLIP_PREROLL_DEFAULT;
      postroll = typeof p.clip_postroll_s === "number" ? p.clip_postroll_s : CLIP_POSTROLL_DEFAULT;
      autoplay = typeof p.clip_autoplay === "boolean" ? p.clip_autoplay : true;
      range = ACTIVITY_RANGES.includes(p.activity_default_range as ActivityRange)
        ? (p.activity_default_range as ActivityRange)
        : "today";
    } catch (e) {
      error = (e as Error).message;
    } finally {
      loading = false;
    }
  });

  // Persist a patch, mirror it into the live playback store, flash "saved".
  async function save(patch: UserPreferences) {
    error = null;
    try {
      const p = await api.patchPreferences(patch);
      playback.setFromPrefs(p); // apply immediately, app-wide
      saved = true;
      clearTimeout(savedTimer);
      savedTimer = setTimeout(() => (saved = false), 1800);
    } catch (e) {
      error = (e as Error).message;
    }
  }

  function setPreroll(v: number) { preroll = v; save({ clip_preroll_s: v }); }
  function setPostroll(v: number) { postroll = v; save({ clip_postroll_s: v }); }
  function setAutoplay(v: boolean) { autoplay = v; save({ clip_autoplay: v }); }
  function setRange(v: ActivityRange) { range = v; save({ activity_default_range: v }); }
</script>

{#if loading}
  <p class="text-baba-text-faint">{t("cameras_loading")}</p>
{:else}
  <div class="max-w-2xl space-y-4">
    {#if error}
      <p class="text-m text-red-400">{error}</p>
    {/if}

    <Card title={t("pref_playback_section")}>
      <div class="space-y-4">
        <div class="flex items-center justify-between gap-4">
          <div class="min-w-0">
            <div class="text-m">{t("pref_clip_preroll")}</div>
            <div class="text-s text-baba-text-faint">{t("pref_clip_preroll_desc")}</div>
          </div>
          <Picks picks={seconds(PREROLL_OPTIONS)} chosen={[String(preroll)]} onpick={(k) => setPreroll(Number(k))} />
        </div>

        <div class="flex items-center justify-between gap-4">
          <div class="min-w-0">
            <div class="text-m">{t("pref_clip_postroll")}</div>
            <div class="text-s text-baba-text-faint">{t("pref_clip_postroll_desc")}</div>
          </div>
          <Picks picks={seconds(POSTROLL_OPTIONS)} chosen={[String(postroll)]} onpick={(k) => setPostroll(Number(k))} />
        </div>

        <div class="flex items-center justify-between gap-4">
          <div class="min-w-0">
            <div class="text-m">{t("pref_clip_autoplay")}</div>
            <div class="text-s text-baba-text-faint">{t("pref_clip_autoplay_desc")}</div>
          </div>
          <Toggle checked={autoplay} label={t("pref_clip_autoplay")} onclick={() => setAutoplay(!autoplay)} />
        </div>
      </div>
    </Card>

    <Card title={t("pref_activity_section")}>
      <div class="flex items-center justify-between gap-4">
        <div class="min-w-0">
          <div class="text-m">{t("pref_activity_range")}</div>
          <div class="text-s text-baba-text-faint">{t("pref_activity_range_desc")}</div>
        </div>
        <Picks
          picks={ACTIVITY_RANGES.map((v) => ({ key: v, label: t(`events_range_${v}` as never) }))}
          chosen={[range]}
          onpick={(k) => setRange(k as ActivityRange)}
        />
      </div>
    </Card>

    {#if saved}
      <div class="text-s text-emerald-400">{t("pref_saved")}</div>
    {/if}
  </div>
{/if}
