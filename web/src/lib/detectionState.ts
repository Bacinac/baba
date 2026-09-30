// How to draw a box from the raw detection feed, given what the pipeline will
// actually do with it: reach the tracker as a full detection, reach it only as
// something that can keep an existing track alive, or stop dead right here.
//
// Two surfaces subscribe to /sse/detections: the zone editor (calibration) and
// the live camera detail view (monitoring). Both are deliberately upstream of
// the tracker, so both carry boxes the pipeline discards, and the operator has
// no way to tell which from the picture alone. The zone editor learned to mark
// them; the live detail view kept painting a suppressed bench as a confident
// yellow "truck 66%" — the exact thing the marking exists to stop, one page
// over. This module is the one answer both of them ask.
//
// The pipeline itself is not re-implemented here. `phantom` is decided
// server-side by the api against the same matcher the tracker uses
// (baba_core.phantom_match) and arrives on the wire; the only thing evaluated
// in the browser is the ignore-zone polygon, and that uses the tracker's
// anchor — the bbox BOTTOM-CENTRE, a subject's foot position — so a polygon
// drawn once behaves the same here as it does in the tracker.
import { t } from "$lib/i18n";
import type { Zone } from "$lib/api";

export type DeadEnd =
  | { kind: "phantom"; births: number; spanH: number }
  | { kind: "ignored" };

// Deliberately not a class hue: a dead-end box must not read as "a dimmer
// truck". Dashed and dimmed, so it stays findable without competing with live
// detections on a bright daylight scene.
export const DEAD_END_COLOR = "#9ca3af";
export const DEAD_END_DASH = [3, 5];
export const DEAD_END_ALPHA = 0.4;

/** Ray casting over a polygon in normalised [0..1] coordinates. */
export function pointInPolygonNorm(x: number, y: number, poly: [number, number][]): boolean {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [xi, yi] = poly[i];
    const [xj, yj] = poly[j];
    const intersect =
      (yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi || 1e-9) + xi;
    if (intersect) inside = !inside;
  }
  return inside;
}

type Det = {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  phantom?: { births: number; span_h: number };
};

/** Why this detection stops here, or null if it reaches the tracker. */
export function deadEnd(
  det: Det,
  zones: Zone[],
  frameWidth: number,
  frameHeight: number,
): DeadEnd | null {
  if (det.phantom) {
    return { kind: "phantom", births: det.phantom.births, spanH: det.phantom.span_h };
  }
  const footX = (det.x1 + det.x2) / 2 / (frameWidth || 1);
  const footY = det.y2 / (frameHeight || 1);
  const ignored = zones.some(
    (z) =>
      z.kind === "ignore" && z.enabled !== false && pointInPolygonNorm(footX, footY, z.polygon),
  );
  return ignored ? { kind: "ignored" } : null;
}

/** Label suffix carrying the reason — and, for a phantom, the evidence.
 *  A greyed box with no reason is just a different kind of mystery. */
export function deadEndLabel(d: DeadEnd): string {
  if (d.kind === "ignored") return ` ⊘ ${t("zoneeditor_det_ignored")}`;
  return ` ⊘ ${t("zoneeditor_det_phantom")} (${d.births}× / ${Math.round(d.spanH)} h)`;
}

// --- maintain-only ---------------------------------------------------------
//
// Everything the api sends has ALREADY passed the real gate: the detector
// publishes down to `min(maintain_conf, min_confidence)` and marks whether the
// box also cleared the BIRTH threshold. So the overlay has nothing left to
// decide — its job is to draw what it was given and show the distinction the
// pipeline actually makes.
//
// It used to invent one instead. Four hardcoded confidence floors (0.30 twice,
// 0.40 twice, in three code paths) filtered the feed a second time against
// numbers that match no configured threshold anywhere. Measured on the patio:
// that silently hid `car` and `motorcycle` boxes which are NOT phantoms, are
// NOT zoned out, and DO reach the tracker — on the one screen whose entire
// purpose is showing what the detector says. The header counted them, the
// canvas did not draw them.
//
// A maintain-only box can keep an existing track alive (a seated person, a
// parked car whose score decayed) but can never start a new one. Real, and
// weaker than a birth-eligible one — so: dimmed and labelled, never hidden.
export const MAINTAIN_ALPHA = 0.55;
export const MAINTAIN_DASH = [1, 3];

export function maintainLabel(): string {
  return ` · ${t("det_maintain_only")}`;
}
