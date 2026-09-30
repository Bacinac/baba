// Shared detection-overlay colors. One palette for every surface that paints
// detector output (ZoneEditor preview, live view, timeline chips) so a class
// reads as the same hue everywhere.
//
// The hues are deliberately NEON-saturated: overlays sit on top of daylight
// video (white concrete, sun-lit walls) where the old tailwind-400 pastels
// washed out completely (operator report, 2026-07-12). Hue positions keep the
// original class→color mapping (person=amber, car=violet, truck=yellow…) so
// nobody has to relearn the legend.

import { t, type MessageKey } from "$lib/i18n";
const PALETTE = [
  "#ffb300", // 0 person   — vivid amber
  "#00e5ff", // 1 bicycle  — electric cyan
  "#bf5aff", // 2 car      — bright violet
  "#00ff88", // 3 motorcycle — spring green
  "#ff2d9b", // 4          — hot pink
  "#ff7a00", // 5 bus, cat=15 — vivid orange
  "#3d8bff", // 6 (dog=16)  — bright blue
  "#ffee00", // 7 truck    — vivid yellow
  "#ff3355", // 8          — bright red
  "#b4ff00", // 9          — lime
];

export function classColor(classId: number): string {
  return PALETTE[classId % PALETTE.length];
}

// Motion-state overrides — distinct from every class hue AND from each other,
// drawn dashed so a parked car still can't be mistaken for a live detection.
// Full-strength colors on purpose: the old grey/55%-alpha treatment made
// parked objects invisible on bright scenes, which defeats "show me what the
// detector says".
export const MOTION_COLORS = {
  stationary: "#ff9100",
  parked: "#00e5ff",
} as const;

// The tracker's terminal stillness state is called `parked` on the wire for
// every class, but "PARKED" is vehicle language — a person or dog that has
// been still past the threshold reads better as "still" (operator report:
// "parked dog"). Display-level only; wire/event kinds keep one vocabulary.
const PARKABLE_IDS = new Set([1, 2, 3, 5, 7]); // bicycle, car, motorcycle, bus, truck
const STATE_KEYS = {
  parked: "live_badge_parked",
  stationary: "live_badge_stationary",
  active: "live_badge_active",
} as const satisfies Record<string, MessageKey>;

export function motionStateLabel(state: string, classId: number): string {
  if (state === "parked" && !PARKABLE_IDS.has(classId)) return t("live_badge_still");
  const key = STATE_KEYS[state as keyof typeof STATE_KEYS];
  return key ? t(key) : state;
}
