// Playback / viewing preferences, applied app-wide. Fed from the server
// prefs blob via auth.svelte.ts `applyServerPrefs` at login/startup so any
// component (the Activity player) can read them without its own fetch.

import type { UserPreferences } from "$lib/api";

export const CLIP_PREROLL_DEFAULT = 5;
export const CLIP_POSTROLL_DEFAULT = 4;
const CLIP_SECS_MAX = 30;

export const ACTIVITY_RANGES = ["hour", "today", "yesterday", "7d", "all"] as const;
export type ActivityRange = (typeof ACTIVITY_RANGES)[number];
export const ACTIVITY_RANGE_DEFAULT: ActivityRange = "today";

function clampSecs(v: unknown, def: number): number {
  return typeof v === "number" && v >= 0 && v <= CLIP_SECS_MAX ? v : def;
}

class PlaybackPrefs {
  /** Seconds of lead-in shown before an event when playing its clip. */
  clipPrerollS = $state(CLIP_PREROLL_DEFAULT);
  /** Seconds of tail shown after an event when playing its clip. */
  clipPostrollS = $state(CLIP_POSTROLL_DEFAULT);
  /** Whether the clip starts playing automatically when an event is opened. */
  autoplay = $state(true);
  /** Default time window when landing on the Activity page. */
  activityRange = $state<ActivityRange>(ACTIVITY_RANGE_DEFAULT);

  /** Apply the full server prefs blob (called at login/startup). */
  setFromPrefs(p: UserPreferences): void {
    this.clipPrerollS = clampSecs(p.clip_preroll_s, CLIP_PREROLL_DEFAULT);
    this.clipPostrollS = clampSecs(p.clip_postroll_s, CLIP_POSTROLL_DEFAULT);
    this.autoplay = typeof p.clip_autoplay === "boolean" ? p.clip_autoplay : true;
    this.activityRange = ACTIVITY_RANGES.includes(p.activity_default_range as ActivityRange)
      ? (p.activity_default_range as ActivityRange)
      : ACTIVITY_RANGE_DEFAULT;
  }
}

export const playback = new PlaybackPrefs();
