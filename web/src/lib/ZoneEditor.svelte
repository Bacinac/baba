<script lang="ts">
  import { dialog, formatNumber, i18n, Button, Card, Tag } from "$lib/kit";
  import { t, type MessageKey } from "$lib/i18n";
  import { onMount, onDestroy } from "svelte";
  import {
    api,
    detectionsFeed,
    tracksFeed,
    type Camera,
    type DetectionsMessage,
    type ProposedZoneRules,
    type TracksMessage,
    type Zone,
    type ZoneClassRule,
    type ZoneObservationPayload,
    type ZoneSuggestion,
    type Feed,
  } from "$lib/api";
  import { classColor, MOTION_COLORS, motionStateLabel } from "$lib/detectionColors";
  import ZoneRulesCard from "$lib/ZoneRulesCard.svelte";
  import LiveStream from "$lib/LiveStream.svelte";
  import { classLabel } from "$lib/classLabels";
  import {
    DEAD_END_ALPHA,
    DEAD_END_COLOR,
    DEAD_END_DASH,
    MAINTAIN_ALPHA,
    MAINTAIN_DASH,
    deadEnd,
    deadEndLabel,
    maintainLabel,
    pointInPolygonNorm,
  } from "$lib/detectionState";

  // --- props / state ------------------------------------------------------

  // Embedded in the camera detail page ("Zone i detekcija" tab); the camera
  // id arrives as a prop rather than from the route, so this editor is just
  // a self-contained widget the parent drops in.
  let { cameraId }: { cameraId: string } = $props();
  let camera = $state<Camera | null>(null);
  let zones = $state<Zone[]>([]);
  let loading = $state(true);
  let error = $state<string | null>(null);

  // Soft tracker — links per-frame detector bboxes across frames via
  // IoU matching so the UI can compute "how long has this thing been
  // here?" without a real track-id feed. Good enough at 5 FPS with
  // pedestrian-speed motion; not a substitute for the server-side
  // tracker but matches its decisions closely for the dwell preview.
  type LiveTrack = {
    id: number;
    classId: number;
    className: string;
    bbox: [number, number, number, number];  // frame coords
    firstSeenMs: number;
    lastSeenMs: number;
    // For each zone, the performance.now() of first sustained entry.
    // Cleared on exit so re-entry resets the clock — same semantics as
    // event-manager's `inside_zones`.
    zoneEntries: Record<string, number>;
  };
  // Plain mutable buffer — NOT $state. The soft-tracker's whole point
  // is to hold per-frame derived data that the canvas redraw consults;
  // it isn't read in any reactive template. Wrapping it in $state was
  // causing an effect_update_depth_exceeded loop because updateSoftTracker
  // both reads and writes it while running inside a $effect on
  // lastMessage. Svelte 5 tracks all state reads transitively, so the
  // write-after-read triggered the effect to fire again on its own
  // output. Plain JS array dodges the tracking entirely; drawDetections
  // sees the latest values at the next tick because it runs on a 125 ms
  // timer (rulesZone preview mode) and on lastMessage changes.
  let liveTracks: LiveTrack[] = [];
  let nextLiveTrackId = 1;

  function iouBox(
    a: [number, number, number, number], b: [number, number, number, number],
  ): number {
    const xx1 = Math.max(a[0], b[0]);
    const yy1 = Math.max(a[1], b[1]);
    const xx2 = Math.min(a[2], b[2]);
    const yy2 = Math.min(a[3], b[3]);
    const iw = Math.max(0, xx2 - xx1);
    const ih = Math.max(0, yy2 - yy1);
    const inter = iw * ih;
    if (inter === 0) return 0;
    const aArea = Math.max(0, a[2] - a[0]) * Math.max(0, a[3] - a[1]);
    const bArea = Math.max(0, b[2] - b[0]) * Math.max(0, b[3] - b[1]);
    return inter / (aArea + bArea - inter);
  }

  function updateSoftTracker(msg: DetectionsMessage) {
    const nowMs = performance.now();
    const used = new Set<number>();
    for (const det of msg.detections) {
      const detBbox: [number, number, number, number] = [det.x1, det.y1, det.x2, det.y2];
      let bestI = -1, bestIou = 0;
      for (let i = 0; i < liveTracks.length; i++) {
        if (used.has(i) || liveTracks[i].classId !== det.class_id) continue;
        const iou = iouBox(liveTracks[i].bbox, detBbox);
        if (iou > bestIou) { bestIou = iou; bestI = i; }
      }
      if (bestI >= 0 && bestIou > 0.30) {
        const lt = liveTracks[bestI];
        lt.bbox = detBbox;
        lt.lastSeenMs = nowMs;
        // Recompute zone membership against current bbox.
        const cxNorm = ((det.x1 + det.x2) / 2) / msg.frame_width;
        const yBotNorm = det.y2 / msg.frame_height;
        const nowInside = new Set<string>();
        for (const z of zones) {
          if (pointInPolygonNorm(cxNorm, yBotNorm, z.polygon)) {
            nowInside.add(z.id);
            if (!(z.id in lt.zoneEntries)) lt.zoneEntries[z.id] = nowMs;
          }
        }
        for (const zid of Object.keys(lt.zoneEntries)) {
          if (!nowInside.has(zid)) delete lt.zoneEntries[zid];
        }
        used.add(bestI);
      } else {
        const cxNorm = ((det.x1 + det.x2) / 2) / msg.frame_width;
        const yBotNorm = det.y2 / msg.frame_height;
        const entries: Record<string, number> = {};
        for (const z of zones) {
          if (pointInPolygonNorm(cxNorm, yBotNorm, z.polygon)) entries[z.id] = nowMs;
        }
        liveTracks.push({
          id: nextLiveTrackId++,
          classId: det.class_id,
          className: det.class_name,
          bbox: detBbox,
          firstSeenMs: nowMs,
          lastSeenMs: nowMs,
          zoneEntries: entries,
        });
      }
    }
    // Drop tracks not seen for >600 ms (= ~3 missed frames at 5 fps).
    if (liveTracks.length > 0) {
      liveTracks = liveTracks.filter((t) => nowMs - t.lastSeenMs < 600);
    }
  }

  $effect(() => { if (lastMessage) updateSoftTracker(lastMessage); });

  // Look up the soft-track for a given detection (same IoU + class
  // match logic, abbreviated). Returns null when nothing matches.
  function findLiveTrackFor(
    det: { x1: number; y1: number; x2: number; y2: number; class_id: number },
  ): LiveTrack | null {
    let best: LiveTrack | null = null;
    let bestIou = 0;
    const detBbox: [number, number, number, number] = [det.x1, det.y1, det.x2, det.y2];
    for (const lt of liveTracks) {
      if (lt.classId !== det.class_id) continue;
      const iou = iouBox(lt.bbox, detBbox);
      if (iou > bestIou) { bestIou = iou; best = lt; }
    }
    return bestIou > 0.30 ? best : null;
  }

  // Live video — replaces the static snapshot. Zone polygons and AI
  // overlays draw over the SAME element, so the SVG sizes itself against
  // the video's bounding box (instead of an <img>'s) — keeps the rest of
  // the drawing/normalization logic untouched.
  // Public demo build: LiveStream renders a blurred still (no live video), so
  // the fps badge would read a permanent "no signal" — hide it there.
  const DEMO = !!import.meta.env.VITE_BABA_DEMO;
  let videoBox = $state<HTMLDivElement | null>(null);
  let videoEl = $state<HTMLVideoElement | null>(null);
  let canvas = $state<HTMLCanvasElement | null>(null);
  let lastMessage = $state<DetectionsMessage | null>(null);
  let detectionsSocket: Feed | null = null;
  // Measured detection frame rate — the rolling rate of detections-message
  // ARRIVALS, i.e. what the adaptive pipeline is actually delivering right
  // now (idle_fps vs target_fps), not the static config number. Refreshed on
  // every message; a ticker marks it stale when the feed pauses.
  let measuredFps = $state<number | null>(null);
  let fpsArrivals: number[] = [];
  let fpsStaleTimer: ReturnType<typeof setInterval> | null = null;
  function noteArrival() {
    const now = performance.now();
    fpsArrivals.push(now);
    if (fpsArrivals.length > 12) fpsArrivals.shift();
    if (fpsArrivals.length >= 3) {
      const spanMs = now - fpsArrivals[0];
      if (spanMs > 0) measuredFps = ((fpsArrivals.length - 1) * 1000) / spanMs;
    }
  }
  // Parallel tracks feed — same camera, carries motion_state + track_id
  // per bbox. The detections feed keeps driving the canvas (every
  // detection, not just tracked ones) but we cross-reference here to render
  // PARKED badges and group same-object bboxes across frames.
  let lastTracksMsg = $state<TracksMessage | null>(null);
  let tracksSocket: Feed | null = null;
  // Kept names `imgEl/imgWidth/imgHeight` so all polygon/click handlers
  // below keep working without renames. `imgEl` now points at the video
  // box rather than an image.
  let imgEl = $state<HTMLElement | null>(null);
  let imgWidth = $state(0);
  let imgHeight = $state(0);
  let resizeObs: ResizeObserver | null = null;

  // Zone-overlay visibility. The filled polygons of a busy zone layout can
  // drown out the live detection bboxes, so the operator can hide the layout
  // while reading what the detector says. Per-browser preference (localStorage)
  // — it's a viewing aid, not camera state. Transient overlays (drawing in
  // progress, VLM suggestions, shape editing) stay visible regardless: the
  // toggle only mutes the SAVED zones.
  let showZones = $state(
    typeof localStorage === "undefined"
      ? true
      : localStorage.getItem("baba.zoneeditor.show_zones") !== "0",
  );
  function toggleZones() {
    showZones = !showZones;
    try {
      localStorage.setItem("baba.zoneeditor.show_zones", showZones ? "1" : "0");
    } catch {
      /* private mode — session-only toggle is fine */
    }
  }

  // Editor / drawing.
  //   view     — looking at the snapshot, zones overlaid
  //   draw     — clicking on the snapshot to add points for a new polygon
  //   meta     — new polygon is finished, filling in name/kind/color before save
  //   shape    — moving vertices of an existing zone
  //   ai-click — user clicks on an object, SAM2 segments it (point prompt)
  //   circle   — click center, drag to set radius, release commits
  //   rect     — click one corner, drag to opposite corner, release commits
  type Mode =
    | "view" | "draw" | "meta" | "shape"
    | "ai-click" | "circle" | "rect";
  let mode = $state<Mode>("view");
  let drawingPts = $state<[number, number][]>([]);
  let savingZone = $state(false);

  // Click-drag state for circle / rect primitives. shapeStart records the
  // first pointerdown; shapeCursor follows the pointer until release.
  let shapeStart = $state<[number, number] | null>(null);
  let shapeCursor = $state<[number, number] | null>(null);

  // AI-click state: accumulated positive + negative points, plus the
  // polygon SAM2 has returned for them. Each new click updates these
  // and re-runs segmentation.
  let aiClickPositive = $state<[number, number][]>([]);
  let aiClickNegative = $state<[number, number][]>([]);
  let aiClickPolygon = $state<[number, number][]>([]);
  let aiClickBusy = $state(false);
  let aiClickError = $state<string | null>(null);
  let aiClassifyBusy = $state(false);


  // New zone form
  let zoneName = $state("");
  let zoneKind = $state<string>("generic");
  let zoneColor = $state("#f59e0b");

  // Per-zone detection rules editor — opens a side panel when the
  // operator clicks the ⚙ button in the zones list.  Independent of
  // shape editing so they can tune rules without entering polygon-edit
  // mode.
  let rulesZoneId = $state<string | null>(null);
  // `rulesZonePreview` is updated by ZoneRulesCard on every slider tick
  // (before the change is committed) so the detection overlay can show
  // the in-flight threshold in real time. Falls back to the saved zone
  // when null.
  let rulesZonePreview = $state<Zone | null>(null);
  let rulesZoneSaved = $derived(
    rulesZoneId ? zones.find((z) => z.id === rulesZoneId) ?? null : null,
  );
  let rulesZone = $derived(rulesZonePreview ?? rulesZoneSaved);

  // Shape edit — moving vertices of an existing zone in place, or dragging the
  // whole zone by its body.
  type ShapeTarget = { kind: "zone"; id: string };
  let shapeTarget = $state<ShapeTarget | null>(null);
  let shapePolygon = $state<[number, number][]>([]);
  let dragVertexIdx = $state<number | null>(null);
  // Whole-zone drag. `anchor` is where the grab started; `snapshot` is the
  // polygon AT that moment — every move re-derives from the snapshot rather
  // than accumulating, because cumulative deltas plus edge clamping drift.
  let dragBodyAnchor = $state<[number, number] | null>(null);
  let dragBodySnapshot: [number, number][] = [];
  let savingShape = $state(false);

  // Toolbar dropdown for primitive shapes (circle / rectangle).
  let shapeMenuOpen = $state(false);

  // Bulk VLM zone suggestion. Hard-wired to Anthropic Claude — GPT-5
  // reasoning models gave unreliable empty / nonsense output in testing.
  let suggestions = $state<ZoneSuggestion[]>([]);
  let suggesting = $state(false);
  let suggestError = $state<string | null>(null);

  // ---- AI auto-tune zone rules --------------------------------------
  // The operator clicks the button; we collect detections for
  // `_TUNE_WINDOW_MS` and aggregate per-zone × per-class confidence /
  // area histograms, then ship them to the backend for analysis. The
  // returned proposals show in a card with per-zone diff vs the current
  // rules, accept/reject individually.
  const _TUNE_WINDOW_MS = 30000;
  type TuneObservation = {
    class_name: string;
    confidence: number;
    area_pct: number;  // bbox area / frame area
  };
  let tuneCollecting = $state(false);
  let tuneRemainingMs = $state(0);
  let tuneAsking = $state(false);
  let tuneError = $state<string | null>(null);
  let tuneProposals = $state<ProposedZoneRules[]>([]);
  // observation buffer: zone_id → list of detections that landed in that
  // zone during the window. We add a synthetic "_outside" bucket so the
  // model also sees noise around (but never gone) zones.
  let tuneBuffer: Record<string, TuneObservation[]> = {};
  let tuneTimer: ReturnType<typeof setTimeout> | null = null;
  let tuneStartedAt = 0;

  // --- data ---------------------------------------------------------------

  async function refresh() {
    loading = true;
    error = null;
    try {
      const [cam, zs] = await Promise.all([
        api.getCamera(cameraId),
        api.listZones(cameraId),
      ]);
      camera = cam;
      zones = zs;
    } catch (e) {
      error = (e as Error).message;
    } finally {
      loading = false;
    }
  }

  function syncImgSize() {
    if (!imgEl) return;
    const r = imgEl.getBoundingClientRect();
    imgWidth = r.width;
    imgHeight = r.height;
    drawDetections();
  }

  // Detection bbox overlay — same code path the /live page uses, painted
  // over the video on a transparent canvas. Zones (SVG) sit above this
  // so they're always visible and clickable. Colors come from the shared
  // neon palette (detectionColors.ts) so every surface paints a class the
  // same hue.

  // Evaluate whether a detection passes the rule for its class within
  // the active zone. Mirrors event-manager's _zone_class_rule logic.
  function passesZoneRule(
    det: { class_name: string; confidence: number; x1: number; y1: number; x2: number; y2: number },
    zone: Zone,
    frameW: number,
    frameH: number,
  ): boolean {
    const rules = zone.rules?.enabled_classes;
    if (!rules) return true;       // No allowlist defined → everything passes
    const rule = rules[det.class_name];
    if (rule === undefined) return false;  // class not in allowlist → reject
    if (rule.min_confidence != null && det.confidence < rule.min_confidence) return false;
    if (rule.min_area_pct != null) {
      const area = ((det.x2 - det.x1) / frameW) * ((det.y2 - det.y1) / frameH);
      if (area < rule.min_area_pct) return false;
    }
    return true;
  }

  function drawDetections() {
    if (!canvas || !videoBox || !videoEl || !lastMessage) return;
    if (videoEl.videoWidth === 0) return;
    const boxRect = videoBox.getBoundingClientRect();
    canvas.width = boxRect.width;
    canvas.height = boxRect.height;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    const imgRect = videoEl.getBoundingClientRect();
    const elemW = imgRect.width;
    const elemH = imgRect.height;
    const natW = videoEl.videoWidth;
    const natH = videoEl.videoHeight;
    const fit = Math.min(elemW / natW, elemH / natH);
    const contentW = natW * fit;
    const contentH = natH * fit;
    const contentX = (imgRect.left - boxRect.left) + (elemW - contentW) / 2;
    const contentY = (imgRect.top - boxRect.top) + (elemH - contentH) / 2;
    const sx = contentW / lastMessage.frame_width;
    const sy = contentH / lastMessage.frame_height;

    // Preview mode kicks in when the operator is tuning a zone — they
    // can immediately see which detections their threshold would let
    // through (drawn solid in the zone's colour) vs. which it would
    // suppress (drawn dashed, dimmed). Outside-zone detections render
    // in muted grey so the eye focuses on the zone being tuned.
    const previewZone: Zone | null = rulesZone;

    // Match each detector bbox to the nearest tracker bbox by IoU so we can
    // look up motion_state AND the track's class-stabilized label. The
    // tracker reports the majority class, so the raw detection class can
    // differ ARBITRARILY from its own track (a carport car reads `dog` one
    // frame) — same-class matches are preferred, but a cross-class match is
    // accepted at a stricter IoU so the flip frames still resolve to their
    // track instead of dangling as unlabeled strangers.
    const trackerBoxes = lastTracksMsg?.tracks ?? [];
    const findMotion = (
      det: { x1: number; y1: number; x2: number; y2: number; class_id: number },
    ): { state: string; sinceNs: number; classId: number; className: string } | null => {
      let bestSame = null as (typeof trackerBoxes)[number] | null;
      let bestSameIou = 0;
      let bestAny = null as (typeof trackerBoxes)[number] | null;
      let bestAnyIou = 0;
      for (const tr of trackerBoxes) {
        const xx1 = Math.max(det.x1, tr.x1);
        const yy1 = Math.max(det.y1, tr.y1);
        const xx2 = Math.min(det.x2, tr.x2);
        const yy2 = Math.min(det.y2, tr.y2);
        const iw = Math.max(0, xx2 - xx1);
        const ih = Math.max(0, yy2 - yy1);
        const inter = iw * ih;
        if (inter === 0) continue;
        const da = (det.x2 - det.x1) * (det.y2 - det.y1);
        const ta = (tr.x2 - tr.x1) * (tr.y2 - tr.y1);
        const iou = inter / (da + ta - inter);
        if (tr.class_id === det.class_id && iou > bestSameIou) {
          bestSameIou = iou;
          bestSame = tr;
        }
        if (iou > bestAnyIou) {
          bestAnyIou = iou;
          bestAny = tr;
        }
      }
      const tr =
        bestSame !== null && bestSameIou > 0.3
          ? bestSame
          : bestAny !== null && bestAnyIou > 0.45
            ? bestAny
            : null;
      if (tr === null) return null;
      return {
        state: tr.motion_state,
        sinceNs: tr.state_since_ns,
        classId: tr.class_id,
        className: tr.class_name,
      };
    };

    // Label anti-collision: adjacent boxes (two cars under the carport) start
    // at the same top edge, so their labels land on the same row and trample
    // each other. Track placed label rects and step a colliding label down a
    // line at a time until it finds air.
    const placedLabels: { x: number; y: number; w: number; h: number }[] = [];
    const LABEL_H = 16;
    const placeLabel = (lx: number, ly: number, lw: number): number => {
      let yTry = ly;
      for (let i = 0; i < 8; i++) {
        const clash = placedLabels.some(
          (r) => lx < r.x + r.w && lx + lw > r.x && yTry - LABEL_H < r.y && yTry > r.y - LABEL_H,
        );
        if (!clash) break;
        yTry += LABEL_H;
      }
      placedLabels.push({ x: lx, y: yTry, w: lw, h: LABEL_H });
      return yTry;
    };

    for (const det of lastMessage.detections) {
      const x = contentX + det.x1 * sx;
      const y = contentY + det.y1 * sy;
      const w = (det.x2 - det.x1) * sx;
      const h = (det.y2 - det.y1) * sy;

      let stroke: string;
      let dash: number[] = [];
      let alpha = 1.0;
      let labelExtra = "";

      // This preview deliberately shows the RAW detector feed, so without a
      // marker a discarded detection looks like a zone that is not working
      // (live: the patio fan, "motorcycle 56%", minutes after the operator
      // drew the zone to kill exactly that). Shared with the live detail
      // view — see deadEnd.ts.
      const dead = deadEnd(det, zones, lastMessage.frame_width, lastMessage.frame_height);

      if (dead) {
        stroke = DEAD_END_COLOR;
        dash = DEAD_END_DASH;
        alpha = DEAD_END_ALPHA;
        labelExtra = deadEndLabel(dead);
      } else if (previewZone) {
        const cxNorm =
          ((det.x1 + det.x2) / 2) / lastMessage.frame_width;
        const yBottomNorm = det.y2 / lastMessage.frame_height;
        const inside = pointInPolygonNorm(cxNorm, yBottomNorm, previewZone.polygon);
        if (inside) {
          // Combine the per-class rule check (confidence + area) with
          // the soft-track dwell check, since both feed into whether
          // event-manager would actually emit zone_enter. Display:
          //   green  — would fire right now
          //   amber  — class+conf ok but still waiting on dwell
          //   red    — class rejected or below min_confidence
          const ruleOk = passesZoneRule(det, previewZone, lastMessage.frame_width, lastMessage.frame_height);
          const lt = findLiveTrackFor(det);
          const rules = previewZone.rules?.enabled_classes?.[det.class_name];
          const dwellGateMs = rules?.min_dwell_ms ?? 0;
          const entryMs = lt?.zoneEntries[previewZone.id];
          const dwellMs = entryMs != null ? (performance.now() - entryMs) : 0;
          const dwellOk = dwellGateMs <= 0 || dwellMs >= dwellGateMs;
          // The backend also runs the motion gate (`_motion_gate_allows`):
          // under "moving_only" a track only fires while its motion state is
          // `active`, and person is exempt. Without it the preview painted a
          // parked car green under a rule that would never have fired for it.
          const motionOk =
            rules?.motion_gate !== "moving_only" ||
            det.class_name.toLowerCase() === "person" ||
            (findMotion(det)?.state ?? "active") === "active";
          if (ruleOk && dwellOk && motionOk) {
            stroke = previewZone.color;
            labelExtra = ` ✓ ${formatNumber(dwellMs / 1000, { minimumFractionDigits: 1, maximumFractionDigits: 1 })}s`;
          } else if (ruleOk && !motionOk) {
            stroke = "#ffb300";
            dash = [4, 3];
            labelExtra = ` ${t("zone_preview_parked")}`;
          } else if (ruleOk && !dwellOk) {
            stroke = "#ffb300";  // vivid amber — waiting on dwell
            dash = [4, 3];
            labelExtra = ` ⏱ ${formatNumber(dwellMs / 1000, { minimumFractionDigits: 1, maximumFractionDigits: 1 })}s/${formatNumber(dwellGateMs / 1000, { minimumFractionDigits: 1, maximumFractionDigits: 1 })}s`;
          } else {
            stroke = "#ff1744";  // vivid red — suppressed by class/conf/area
            dash = [6, 4];
            labelExtra = " ✕";
          }
        } else {
          // Outside the zone being tuned — de-emphasized but still findable
          // (the old zinc-500 @ 0.45 vanished on bright scenes).
          stroke = "#e4e4e7";
          alpha = 0.75;
        }
      } else {
        stroke = classColor(det.class_id);
      }

      // Motion-state badge — overrides palette colour when the
      // tracker says this object has parked. Stale jitter on a still
      // car would otherwise paint every frame as a fresh "person 95%"
      // detection; here we collapse all of those into one greyed-out
      // "PARKED 2m 14s" box. The "since" timer ticks live because
      // drawDetections runs on the 8 Hz preview ticker.
      const motion = findMotion(det);
      // Class shown to the operator: the track's class-stabilized label when
      // we have a matching track (a carport car stays "car" even while the
      // detector's frame flips to "motorcycle"). Zone-rule tuning keeps the
      // RAW detection class — rules evaluate what the detector actually said.
      let displayName = det.class_name;
      if (!previewZone && !dead && motion) {
        displayName = classLabel(motion.className);
        stroke = classColor(motion.classId);
      }
      let motionLabel = "";
      if (motion && motion.state !== "active") {
        const ageSec = motion.sinceNs
          ? Math.max(0, (Date.now() * 1e6 - motion.sinceNs) / 1e9)
          : 0;
        const ageStr = ageSec >= 60
          ? `${Math.floor(ageSec / 60)}m ${Math.floor(ageSec % 60)}s`
          : `${formatNumber(ageSec, { maximumFractionDigits: 0 })}s`;
        motionLabel = ` · ${motionStateLabel(motion.state, motion.classId)} ${ageStr}`;
        // Override colour so parked/stationary read differently from live
        // detections — but at FULL strength (the old grey + 0.55 alpha was
        // invisible on bright scenes). The dash carries the "not active"
        // semantics; the colour just has to be findable.
        if (!previewZone) {
          stroke = motion.state === "parked" ? MOTION_COLORS.parked : MOTION_COLORS.stationary;
          dash = [4, 3];
        }
      }
      // Below the BIRTH threshold but above the maintain floor: real, and it
      // reaches the tracker — but it can only keep an existing track alive,
      // never start one. Dimmed and labelled, because the pipeline makes this
      // distinction and the operator calibrating against it needs to see it.
      if (!dead && det.birth_eligible === false) {
        alpha = MAINTAIN_ALPHA;
        if (dash.length === 0) dash = MAINTAIN_DASH;
        motionLabel += maintainLabel();
      }

      ctx.globalAlpha = alpha;
      // High-contrast rendering: a dark halo under the coloured stroke and an
      // outlined label keep the bbox readable over bright daylight scenes and
      // zone fills alike — a bare 2px coloured line disappears against both.
      ctx.setLineDash(dash);
      ctx.strokeStyle = "rgba(0,0,0,0.85)";
      ctx.lineWidth = 4.5;
      ctx.strokeRect(x, y, w, h);
      ctx.strokeStyle = stroke;
      ctx.lineWidth = 2.5;
      ctx.strokeRect(x, y, w, h);
      ctx.setLineDash([]);
      const label = `${displayName} ${Math.round(det.confidence * 100)}%${labelExtra}${motionLabel}`;
      ctx.font = "600 13px ui-sans-serif, system-ui, sans-serif";
      const labelY = placeLabel(x + 4, y + 15, ctx.measureText(label).width);
      ctx.lineJoin = "round";
      ctx.lineWidth = 3;
      ctx.strokeStyle = "rgba(0,0,0,0.9)";
      ctx.strokeText(label, x + 4, labelY);
      ctx.fillStyle = stroke;
      ctx.fillText(label, x + 4, labelY);
      ctx.globalAlpha = 1.0;
    }
  }

  // Redraw whenever a new detections message arrives OR when the
  // operator changes zones-rules state — the preview must follow live.
  $effect(() => {
    void lastMessage;
    void lastTracksMsg;
    void rulesZone;
    void rulesZone?.rules;
    drawDetections();
  });

  // While a zone is being tuned the dwell counters need to advance
  // between detection frames (otherwise they only step every ~200 ms).
  // 8 Hz redraw is smooth enough for the operator's eye and costs
  // ~6× the drawDetections work, which is still trivial.
  let dwellTicker: ReturnType<typeof setInterval> | null = null;
  $effect(() => {
    if (rulesZone) {
      if (dwellTicker === null) {
        dwellTicker = setInterval(() => drawDetections(), 125);
      }
    } else if (dwellTicker !== null) {
      clearInterval(dwellTicker);
      dwellTicker = null;
    }
    return () => {
      if (dwellTicker !== null) {
        clearInterval(dwellTicker);
        dwellTicker = null;
      }
    };
  });

  // --- drawing ------------------------------------------------------------

  function startDraw() {
    mode = "draw";
    drawingPts = [];
    zoneName = "";
    zoneKind = "generic";
    zoneColor = "#f59e0b";
  }

  function cancelDraw() {
    mode = "view";
    drawingPts = [];
  }

  function onSvgClick(e: MouseEvent) {
    if (!imgEl || imgWidth === 0 || imgHeight === 0) return;
    const rect = imgEl.getBoundingClientRect();
    const x = (e.clientX - rect.left) / rect.width;
    const y = (e.clientY - rect.top) / rect.height;
    if (x < 0 || x > 1 || y < 0 || y > 1) return;
    if (mode === "draw") {
      drawingPts = [...drawingPts, [x, y]];
    } else if (mode === "ai-click") {
      // Shift = additional positive (refine "include this too").
      // Alt   = negative (refine "exclude this").
      // Plain = reset to a single positive click.
      if (e.altKey) {
        aiClickNegative = [...aiClickNegative, [x, y]];
      } else if (e.shiftKey) {
        aiClickPositive = [...aiClickPositive, [x, y]];
      } else {
        aiClickPositive = [[x, y]];
        aiClickNegative = [];
      }
      void runAiClick();
    }
  }

  function onSvgDblClick(e: MouseEvent) {
    if (mode !== "draw") return;
    e.preventDefault();
    finishDraw();
  }

  function onKeyDown(e: KeyboardEvent) {
    if (mode === "draw") {
      if (e.key === "Enter") finishDraw();
      else if (e.key === "Escape") cancelDraw();
      else if (e.key === "Backspace" && drawingPts.length > 0) {
        drawingPts = drawingPts.slice(0, -1);
      }
    } else if (mode === "shape") {
      if (e.key === "Escape") cancelShape();
      else if (e.key === "Enter" && shapePolygon.length >= 3) saveShape();
    } else if (mode === "ai-click") {
      if (e.key === "Escape") cancelAiClick();
    } else if (mode === "circle" || mode === "rect") {
      if (e.key === "Escape") cancelPrimitive();
    }
  }

  function finishDraw() {
    if (drawingPts.length < 3) return;
    // Pre-fill a sensible default name; user can change in the form.
    if (!zoneName) zoneName = `Zona ${zones.length + 1}`;
    mode = "meta";
  }

  async function saveDrawn() {
    if (drawingPts.length < 3) return;
    savingZone = true;
    try {
      const z = await api.createZone(cameraId, {
        name: zoneName.trim() || `Zona ${zones.length + 1}`,
        kind: zoneKind,
        polygon: drawingPts,
        color: zoneColor,
        enabled: true,
      });
      zones = [...zones, z];
      mode = "view";
      drawingPts = [];
    } catch (e) {
      error = (e as Error).message;
    } finally {
      savingZone = false;
    }
  }

  // --- Primitive shape drawing (circle, rectangle) ------------------------

  function startCircle() {
    if (mode !== "view") return;
    mode = "circle";
    shapeStart = null;
    shapeCursor = null;
    zoneName = "";
    zoneKind = "generic";
    zoneColor = "#f59e0b";
  }

  function startRect() {
    if (mode !== "view") return;
    mode = "rect";
    shapeStart = null;
    shapeCursor = null;
    zoneName = "";
    zoneKind = "generic";
    zoneColor = "#f59e0b";
  }

  function cancelPrimitive() {
    mode = "view";
    shapeStart = null;
    shapeCursor = null;
  }

  /** Pointer → normalised [0,1] frame coords, REJECTING anything outside the
   *  picture. For click-to-place: a click next to the video must not drop a
   *  point on its edge. */
  function pointerToNorm(e: PointerEvent): [number, number] | null {
    if (!imgEl || imgWidth === 0 || imgHeight === 0) return null;
    const rect = imgEl.getBoundingClientRect();
    const x = (e.clientX - rect.left) / rect.width;
    const y = (e.clientY - rect.top) / rect.height;
    if (x < 0 || x > 1 || y < 0 || y > 1) return null;
    return [x, y];
  }

  /** Same mapping, CLAMPED to the picture instead of rejected. For drags: a
   *  gesture that wanders off the video must keep tracking along the edge —
   *  rejecting would freeze the shape mid-drag and snap it back on re-entry.
   *  (The vertex drag used to inline this maths for exactly this reason.) */
  function pointerToNormClamped(e: PointerEvent): [number, number] | null {
    if (!imgEl || imgWidth === 0 || imgHeight === 0) return null;
    const rect = imgEl.getBoundingClientRect();
    return [
      Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width)),
      Math.max(0, Math.min(1, (e.clientY - rect.top) / rect.height)),
    ];
  }

  function onSvgPointerDown(e: PointerEvent) {
    if (mode !== "circle" && mode !== "rect") return;
    if (e.button !== 0) return;
    const p = pointerToNorm(e);
    if (!p) return;
    e.preventDefault();
    (e.currentTarget as Element).setPointerCapture?.(e.pointerId);
    shapeStart = p;
    shapeCursor = p;
  }

  function onSvgPointerMove(e: PointerEvent) {
    if (mode !== "circle" && mode !== "rect") return;
    if (!shapeStart) return;
    const p = pointerToNorm(e);
    if (!p) return;
    shapeCursor = p;
  }

  function onSvgPointerUp(e: PointerEvent) {
    if (mode !== "circle" && mode !== "rect") return;
    if (!shapeStart || !shapeCursor) {
      shapeStart = null;
      shapeCursor = null;
      return;
    }
    (e.currentTarget as Element).releasePointerCapture?.(e.pointerId);

    const dx = (shapeCursor[0] - shapeStart[0]) * imgWidth;
    const dy = (shapeCursor[1] - shapeStart[1]) * imgHeight;
    const minPx = 8;
    if (Math.hypot(dx, dy) < minPx) {
      shapeStart = null;
      shapeCursor = null;
      return;
    }

    const poly = mode === "circle"
      ? circlePolygon(shapeStart, shapeCursor)
      : rectPolygon(shapeStart, shapeCursor);
    drawingPts = poly;
    shapeStart = null;
    shapeCursor = null;
    if (!zoneName) zoneName = `Zona ${zones.length + 1}`;
    mode = "meta";
  }

  function circlePolygon(
    center: [number, number], edge: [number, number],
  ): [number, number][] {
    // Generate a 32-gon. Work in pixels so the circle looks circular even
    // when the image isn't square, then re-normalize to [0, 1].
    const cxPx = center[0] * imgWidth;
    const cyPx = center[1] * imgHeight;
    const exPx = edge[0] * imgWidth;
    const eyPx = edge[1] * imgHeight;
    const rPx = Math.hypot(exPx - cxPx, eyPx - cyPx);
    const N = 32;
    const out: [number, number][] = [];
    for (let i = 0; i < N; i++) {
      const t = (i / N) * Math.PI * 2;
      const x = (cxPx + rPx * Math.cos(t)) / imgWidth;
      const y = (cyPx + rPx * Math.sin(t)) / imgHeight;
      out.push([
        Math.max(0, Math.min(1, x)),
        Math.max(0, Math.min(1, y)),
      ]);
    }
    return out;
  }

  function rectPolygon(
    a: [number, number], b: [number, number],
  ): [number, number][] {
    const x1 = Math.min(a[0], b[0]);
    const x2 = Math.max(a[0], b[0]);
    const y1 = Math.min(a[1], b[1]);
    const y2 = Math.max(a[1], b[1]);
    return [[x1, y1], [x2, y1], [x2, y2], [x1, y2]];
  }

  // --- AI click segmentation ----------------------------------------------

  function startAiClick() {
    if (mode !== "view") return;
    mode = "ai-click";
    aiClickPositive = [];
    aiClickNegative = [];
    aiClickPolygon = [];
    aiClickError = null;
    zoneName = "";
    zoneKind = "generic";
    zoneColor = "#f59e0b";
  }

  function cancelAiClick() {
    mode = "view";
    aiClickPositive = [];
    aiClickNegative = [];
    aiClickPolygon = [];
    aiClickError = null;
  }

  async function runAiClick() {
    if (aiClickPositive.length === 0) {
      aiClickPolygon = [];
      return;
    }
    aiClickBusy = true;
    aiClickError = null;
    try {
      const res = await api.segmentAtPoint(cameraId, {
        positive: aiClickPositive,
        negative: aiClickNegative,
        // Iterative refine: pass the previous polygon so SAM2 adjusts
        // it rather than starting over. Skip on a fresh single-click
        // (the user just reset) — that's a "from scratch" request.
        prev_polygon:
          aiClickPositive.length + aiClickNegative.length > 1 && aiClickPolygon.length >= 3
            ? aiClickPolygon
            : null,
      });
      if (res.error) {
        aiClickError = res.error;
        aiClickPolygon = [];
      } else if (res.polygon.length < 3) {
        aiClickError = t("zone_ai_click_no_mask");
        aiClickPolygon = [];
      } else {
        aiClickPolygon = res.polygon;
      }
    } catch (e) {
      aiClickError = (e as Error).message;
    } finally {
      aiClickBusy = false;
    }
  }

  async function aiClassifyCurrent() {
    if (aiClickPolygon.length < 3) return;
    aiClassifyBusy = true;
    try {
      const res = await api.classifyPolygon(cameraId, aiClickPolygon, i18n.locale);
      if (res.error) {
        aiClickError = res.error;
        return;
      }
      if (res.name) zoneName = res.name;
      if (res.kind) zoneKind = res.kind;
    } catch (e) {
      aiClickError = (e as Error).message;
    } finally {
      aiClassifyBusy = false;
    }
  }

  // --- VLM bulk suggest (provider-pickable for A/B testing) ---------------

  // Each detections-WS message arriving while we're collecting feeds
  // into the per-zone buckets. point-in-polygon on the bbox bottom
  // centre matches event-manager's anchor convention.
  function recordTuneObservation(msg: DetectionsMessage) {
    if (!tuneCollecting) return;
    const fw = msg.frame_width;
    const fh = msg.frame_height;
    if (fw === 0 || fh === 0) return;
    for (const det of msg.detections) {
      const cx = ((det.x1 + det.x2) / 2) / fw;
      const yBot = det.y2 / fh;
      const area = ((det.x2 - det.x1) * (det.y2 - det.y1)) / (fw * fh);
      let landed = false;
      for (const z of zones) {
        if (pointInPolygonNorm(cx, yBot, z.polygon)) {
          (tuneBuffer[z.id] ??= []).push({
            class_name: det.class_name,
            confidence: det.confidence,
            area_pct: area,
          });
          landed = true;
        }
      }
      if (!landed) {
        // Optional: track outside-zone noise for visibility. Backend
        // ignores unknown zone_ids but the count is useful for the
        // operator to know whether the camera was active during sampling.
      }
    }
  }

  $effect(() => {
    if (lastMessage) recordTuneObservation(lastMessage);
  });

  function quantile(sorted: number[], q: number): number {
    if (sorted.length === 0) return 0;
    const idx = (sorted.length - 1) * q;
    const lo = Math.floor(idx);
    const hi = Math.ceil(idx);
    if (lo === hi) return sorted[lo];
    return sorted[lo] * (hi - idx) + sorted[hi] * (idx - lo);
  }

  function summarizeBuffer(): ZoneObservationPayload[] {
    return zones.map((z) => {
      const obs = tuneBuffer[z.id] ?? [];
      const byClass: Record<string, TuneObservation[]> = {};
      for (const o of obs) (byClass[o.class_name] ??= []).push(o);
      const stats: Record<string, {
        count: number; conf_p25: number; conf_p50: number;
        conf_p75: number; conf_max: number; area_p50: number;
      }> = {};
      for (const [cls, arr] of Object.entries(byClass)) {
        const confs = arr.map((o) => o.confidence).sort((a, b) => a - b);
        const areas = arr.map((o) => o.area_pct).sort((a, b) => a - b);
        stats[cls] = {
          count: arr.length,
          conf_p25: quantile(confs, 0.25),
          conf_p50: quantile(confs, 0.50),
          conf_p75: quantile(confs, 0.75),
          conf_max: confs[confs.length - 1],
          area_p50: quantile(areas, 0.50),
        };
      }
      return {
        zone_id: z.id,
        zone_name: z.name,
        zone_kind: z.kind,
        current_rules: z.rules ?? null,
        class_stats: stats,
      };
    });
  }

  function startTune() {
    if (tuneCollecting || tuneAsking) return;
    tuneError = null;
    tuneProposals = [];
    tuneBuffer = {};
    tuneCollecting = true;
    tuneStartedAt = performance.now();
    tuneRemainingMs = _TUNE_WINDOW_MS;
    const tick = () => {
      const elapsed = performance.now() - tuneStartedAt;
      tuneRemainingMs = Math.max(0, _TUNE_WINDOW_MS - elapsed);
      if (tuneRemainingMs <= 0) {
        finishTune();
      } else {
        tuneTimer = setTimeout(tick, 200);
      }
    };
    tuneTimer = setTimeout(tick, 200);
  }

  function cancelTune() {
    if (tuneTimer) clearTimeout(tuneTimer);
    tuneTimer = null;
    tuneCollecting = false;
    tuneRemainingMs = 0;
    tuneBuffer = {};
  }

  async function finishTune() {
    if (tuneTimer) clearTimeout(tuneTimer);
    tuneTimer = null;
    tuneCollecting = false;
    const fw = lastMessage?.frame_width ?? 0;
    const fh = lastMessage?.frame_height ?? 0;
    if (!fw || !fh) {
      tuneError = t("zone_tune_err_no_detections");
      return;
    }
    tuneAsking = true;
    try {
      const res = await api.tuneZoneRules(cameraId, {
        duration_s: _TUNE_WINDOW_MS / 1000,
        frame_width: fw,
        frame_height: fh,
        zones: summarizeBuffer(),
      });
      if (res.error) {
        tuneError = res.error;
      } else if (res.proposals.length === 0) {
        tuneError = `${res.model} ${t("zone_tune_err_no_proposals")} (${res.latency_ms}ms). ${t("zone_tune_err_too_few")}`;
      } else {
        tuneProposals = res.proposals;
      }
    } catch (e) {
      tuneError = (e as Error).message;
    } finally {
      tuneAsking = false;
    }
  }

  async function applyProposal(p: ProposedZoneRules) {
    try {
      const enabled: Record<string, ZoneClassRule> = {};
      for (const [cls, rule] of Object.entries(p.enabled_classes)) {
        enabled[cls] = {
          ...(rule.min_confidence !== null ? { min_confidence: rule.min_confidence } : {}),
          ...(rule.min_area_pct !== null ? { min_area_pct: rule.min_area_pct } : {}),
          ...(rule.min_dwell_ms !== null ? { min_dwell_ms: rule.min_dwell_ms } : {}),
          ...(rule.cooldown_s !== null ? { cooldown_s: rule.cooldown_s } : {}),
        };
      }
      const updated = await api.patchZone(p.zone_id, {
        rules: { enabled_classes: enabled },
      });
      zones = zones.map((z) => (z.id === updated.id ? updated : z));
      tuneProposals = tuneProposals.filter((x) => x.zone_id !== p.zone_id);
    } catch (e) {
      tuneError = (e as Error).message;
    }
  }

  function dismissProposal(p: ProposedZoneRules) {
    tuneProposals = tuneProposals.filter((x) => x.zone_id !== p.zone_id);
  }

  // provider undefined → the API auto-picks the first enabled provider
  // (Anthropic preferred when both are on; disable one to route to the other).
  // Hardcoding "anthropic" here broke OpenAI-only installs with a hard error.
  async function suggestZonesWith(provider?: string) {
    const label = provider ?? "AI";
    suggesting = true;
    suggestError = null;
    suggestions = [];
    try {
      const res = await api.suggestZones(cameraId, i18n.locale, provider);
      if (res.error) {
        suggestError = `${label}: ${res.error}`;
      } else if (res.suggestions.length === 0) {
        // 200 OK but no zones — model didn't propose anything (empty
        // JSON, or parser dropped all entries). Show a visible message
        // so the operator knows it wasn't a silent failure.
        suggestError = `${res.model || label}: ${t("zone_tune_err_no_proposals")} (${res.latency_ms}ms)`;
      } else {
        suggestions = res.suggestions;
      }
    } catch (e) {
      suggestError = `${label}: ${(e as Error).message}`;
    } finally {
      suggesting = false;
    }
  }

  async function acceptSuggestion(idx: number) {
    const s = suggestions[idx];
    try {
      const z = await api.createZone(cameraId, {
        name: s.name,
        kind: s.kind,
        polygon: s.polygon,
        color: s.color,
        enabled: true,
      });
      zones = [...zones, z];
      suggestions = suggestions.filter((_, i) => i !== idx);
    } catch (e) {
      error = (e as Error).message;
    }
  }

  function dismissSuggestion(idx: number) {
    suggestions = suggestions.filter((_, i) => i !== idx);
  }

  function dismissAllSuggestions() {
    suggestions = [];
  }

  async function saveAiClick() {
    if (aiClickPolygon.length < 3) return;
    savingZone = true;
    try {
      const z = await api.createZone(cameraId, {
        name: zoneName.trim() || `Zona ${zones.length + 1}`,
        kind: zoneKind,
        polygon: aiClickPolygon,
        color: zoneColor,
        enabled: true,
      });
      zones = [...zones, z];
      cancelAiClick();
    } catch (e) {
      error = (e as Error).message;
    } finally {
      savingZone = false;
    }
  }

  // --- shape editing ------------------------------------------------------

  function startEditZone(z: Zone) {
    if (mode !== "view") return;
    shapeTarget = { kind: "zone", id: z.id };
    shapePolygon = z.polygon.map(([x, y]) => [x, y] as [number, number]);
    // Pre-fill the meta form too so the user can edit name/kind/color
    // alongside the polygon. saveShape sends whichever fields changed.
    // The SVG reads zoneColor/zoneName directly, so retyping the name or
    // picking a colour updates the overlay live — there used to be a second
    // pair of shapeColor/shapeLabel copies here that were set once and never
    // re-synced, so the drawing kept showing the OLD name and colour while the
    // form showed the new ones.
    zoneName = z.name;
    zoneKind = z.kind;
    zoneColor = z.color;
    mode = "shape";
  }

  function cancelShape() {
    mode = "view";
    shapeTarget = null;
    shapePolygon = [];
    dragVertexIdx = null;
    dragBodyAnchor = null;
    dragBodySnapshot = [];
  }

  async function saveShape() {
    if (shapePolygon.length < 3 || !shapeTarget) return;
    savingShape = true;
    try {
      const updated = await api.patchZone(shapeTarget.id, {
        polygon: shapePolygon,
        name: zoneName.trim() || undefined,
        kind: zoneKind,
        color: zoneColor,
      });
      zones = zones.map((z) => (z.id === updated.id ? updated : z));
      cancelShape();
    } catch (e) {
      error = (e as Error).message;
    } finally {
      savingShape = false;
    }
  }

  function onVertexPointerDown(i: number, e: PointerEvent) {
    if (mode !== "shape") return;
    // Right click — context menu handler removes the vertex; let it through.
    if (e.button !== 0) return;
    e.preventDefault();
    e.stopPropagation();
    (e.currentTarget as Element).setPointerCapture(e.pointerId);
    dragVertexIdx = i;
  }

  function onVertexPointerMove(e: PointerEvent) {
    if (mode !== "shape" || dragVertexIdx === null) return;
    const p = pointerToNormClamped(e);
    if (!p) return;
    const next = shapePolygon.slice();
    next[dragVertexIdx] = p;
    shapePolygon = next;
  }

  // --- whole-zone drag ----------------------------------------------------
  // Same pointer-capture idiom as the vertex handles above; the vertices are
  // drawn AFTER the body, so they win the hit-test and grabbing a handle still
  // moves just that point.

  function onPolyPointerDown(e: PointerEvent) {
    if (mode !== "shape" || e.button !== 0) return;
    const p = pointerToNormClamped(e);
    if (!p) return;
    e.preventDefault();
    e.stopPropagation();
    (e.currentTarget as Element).setPointerCapture?.(e.pointerId);
    dragBodyAnchor = p;
    dragBodySnapshot = shapePolygon.map(([x, y]) => [x, y] as [number, number]);
  }

  function onPolyPointerMove(e: PointerEvent) {
    if (mode !== "shape" || !dragBodyAnchor || dragBodySnapshot.length === 0) return;
    const p = pointerToNormClamped(e);
    if (!p) return;
    // Clamp the translation VECTOR against the polygon's bounding box — not
    // each vertex separately. Per-vertex clamping would squash the zone flat
    // against the frame edge instead of stopping it there; the shape must stay
    // rigid while it moves.
    const xs = dragBodySnapshot.map((q) => q[0]);
    const ys = dragBodySnapshot.map((q) => q[1]);
    const dx = Math.max(-Math.min(...xs), Math.min(p[0] - dragBodyAnchor[0], 1 - Math.max(...xs)));
    const dy = Math.max(-Math.min(...ys), Math.min(p[1] - dragBodyAnchor[1], 1 - Math.max(...ys)));
    shapePolygon = dragBodySnapshot.map(([x, y]) => [x + dx, y + dy] as [number, number]);
  }

  function onPolyPointerUp(e: PointerEvent) {
    if (!dragBodyAnchor) return;
    const el = e.currentTarget as Element;
    if (el && "releasePointerCapture" in el) {
      try { el.releasePointerCapture(e.pointerId); } catch { /* already released */ }
    }
    dragBodyAnchor = null;
    dragBodySnapshot = [];
  }

  function onVertexPointerUp(e: PointerEvent) {
    if (dragVertexIdx === null) return;
    const el = e.currentTarget as Element;
    if (el && "releasePointerCapture" in el) {
      try { el.releasePointerCapture(e.pointerId); } catch { /* already released */ }
    }
    dragVertexIdx = null;
  }

  function onVertexContextMenu(i: number, e: MouseEvent) {
    if (mode !== "shape") return;
    e.preventDefault();
    e.stopPropagation();
    if (shapePolygon.length <= 3) return;
    shapePolygon = shapePolygon.filter((_, j) => j !== i);
  }

  function onMidpointClick(i: number, e: MouseEvent) {
    if (mode !== "shape") return;
    e.preventDefault();
    e.stopPropagation();
    const a = shapePolygon[i];
    const b = shapePolygon[(i + 1) % shapePolygon.length];
    const mid: [number, number] = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
    const next = shapePolygon.slice();
    next.splice(i + 1, 0, mid);
    shapePolygon = next;
  }

  // --- zone list actions --------------------------------------------------

  async function deleteZone(z: Zone) {
    const ok = await dialog.confirm({
      title: t("dialog_confirm_title"),
      message: `${t("zone_confirm_delete")} "${z.name}"?`,
      confirmLabel: t("dialog_delete"),
      danger: true,
    });
    if (!ok) return;
    try {
      await api.deleteZone(z.id);
      zones = zones.filter((x) => x.id !== z.id);
      if (shapeTarget?.id === z.id) {
        // The shape editor holds a COPY of the polygon, so nothing else would
        // notice the zone is gone: the handles stay on screen and Save would
        // PATCH a deleted id (404 dumped raw into the error line). Deleting the
        // zone you are editing is now easy to do — you can enter shape mode by
        // clicking the zone itself, so this is the common state to be in.
        cancelShape();
      }
    } catch (e) {
      error = (e as Error).message;
    }
  }

  async function toggleZoneEnabled(z: Zone) {
    try {
      const updated = await api.patchZone(z.id, { enabled: !z.enabled });
      zones = zones.map((x) => (x.id === z.id ? updated : x));
    } catch (e) {
      error = (e as Error).message;
    }
  }

  // --- helpers ------------------------------------------------------------

  function pointsAttr(poly: [number, number][]): string {
    return poly.map(([x, y]) => `${x * imgWidth},${y * imgHeight}`).join(" ");
  }

  /** Shoelace area of a normalised polygon (absolute, so winding doesn't matter). */
  function polygonArea(poly: [number, number][]): number {
    let a = 0;
    for (let i = 0; i < poly.length; i++) {
      const [x1, y1] = poly[i];
      const [x2, y2] = poly[(i + 1) % poly.length];
      a += x1 * y2 - x2 * y1;
    }
    return Math.abs(a) / 2;
  }

  /** Zones ordered largest-first for rendering. SVG hit-testing returns the
   *  TOPMOST element, and a zone's fill stays hit-testable however transparent
   *  it is — so in document order a big zone swallows every click meant for the
   *  small ones inside it, and they could never be selected. Drawing the
   *  smallest last puts them on top, which also just reads better. */
  let zonesByArea = $derived(
    [...zones].sort((a, b) => polygonArea(b.polygon) - polygonArea(a.polygon)),
  );

  const KIND_KEYS: { value: string; key: MessageKey }[] = [
    { value: "entry",      key: "zone_kind_entry" },
    { value: "exit",       key: "zone_kind_exit" },
    { value: "restricted", key: "zone_kind_restricted" },
    { value: "parking",    key: "zone_kind_parking" },
    { value: "no_go",      key: "zone_kind_no_go" },
    { value: "interest",   key: "zone_kind_interest" },
    { value: "generic",    key: "zone_kind_generic" },
    // Last, and labelled "(ne prati)" / "(don't track)" on purpose: every
    // other kind ADDS events, this one removes them. It sits one line below
    // "Zabranjena zona", which sounds like a synonym and is its opposite —
    // picking it by mistake blinds that ground instead of alarming on it.
    { value: "ignore",     key: "zone_kind_ignore" },
  ];

  function kindLabel(kind: string): string {
    const m = KIND_KEYS.find((k) => k.value === kind);
    return m ? t(m.key) : kind;
  }

  // --- lifecycle ----------------------------------------------------------

  onMount(() => {
    refresh();
    window.addEventListener("keydown", onKeyDown);
    if (typeof ResizeObserver !== "undefined") {
      resizeObs = new ResizeObserver(() => syncImgSize());
    }
  });

  // Subscribe to the per-camera detections websocket once we know the
  // slug — the camera detail loads from getCamera and that gives us a
  // slug. Re-subscribe only when slug actually changes (mount-time
  // navigation, not re-renders).
  let subscribedSlug: string | null = null;
  $effect(() => {
    const slug = camera?.slug;
    if (!slug || slug === subscribedSlug) return;
    if (detectionsSocket) {
      detectionsSocket.close();
    }
    subscribedSlug = slug;
    detectionsSocket = detectionsFeed(slug, {
      message: (m) => {
        lastMessage = m;
        noteArrival();
      },
    });
    measuredFps = null;
    fpsArrivals = [];
    if (fpsStaleTimer === null) {
      fpsStaleTimer = setInterval(() => {
        const last = fpsArrivals[fpsArrivals.length - 1];
        // 5 s without a frame even at idle 1 fps means the feed is stalled —
        // show "no signal" instead of a frozen number.
        if (last !== undefined && performance.now() - last > 5000) {
          measuredFps = null;
          fpsArrivals = [];
        }
      }, 2000);
    }

    // Parallel tracker feed — same camera, carries motion_state per
    // bbox so PARKED labels reflect the server's view, not a client-
    // side guess. Keeps the separate detections feed in place because
    // tracker filters by class (person/vehicle only); detector still
    // surfaces everything else for the live overlay.
    if (tracksSocket) {
      tracksSocket.close();
    }
    tracksSocket = tracksFeed(slug, { message: (m) => (lastTracksMsg = m) });
  });

  // Keep `imgEl` (the legacy name the polygon/click handlers use)
  // pointed at the video box element. Updating it via $effect avoids the
  // Svelte-5 limitation that bind:this doesn't accept an arrow callback.
  $effect(() => { imgEl = videoBox; });

  // Drop the in-flight preview whenever the operator switches to a
  // different zone or closes the editor — otherwise stale slider state
  // from the previous zone would tint the overlay on the new one.
  $effect(() => { void rulesZoneId; rulesZonePreview = null; });

  $effect(() => {
    if (imgEl && resizeObs) {
      resizeObs.observe(imgEl);
      return () => resizeObs?.unobserve(imgEl!);
    }
  });

  onDestroy(() => {
    window.removeEventListener("keydown", onKeyDown);
    resizeObs?.disconnect();
    // Stop the auto-tune sampling chain — it self-reschedules for ~30s and
    // would otherwise fire finishTune() (an AI API call) after unmount, writing
    // to dead component state.
    if (tuneTimer) { clearTimeout(tuneTimer); tuneTimer = null; }
    if (detectionsSocket) {
      detectionsSocket.close();
    }
    if (tracksSocket) {
      tracksSocket.close();
    }
    if (fpsStaleTimer !== null) {
      clearInterval(fpsStaleTimer);
      fpsStaleTimer = null;
    }
  });
</script>

{#if loading}
  <p class="text-baba-text-faint">{t("cameras_loading")}</p>
{:else if error}
  <p class="text-red-400">{error}</p>
{:else if camera}
  <div class="space-y-4">
    <!-- Live stream + zone editor overlay (full width). Detection bboxes
         paint on the canvas; zones + drawing handles on the SVG. -->
    <div
      bind:this={videoBox}
      class="relative w-full overflow-hidden rounded-lg border border-baba-border bg-black"
    >
      {#if !DEMO}
        <div
          class="pointer-events-none absolute right-2 top-2 z-10 rounded bg-black/65 px-1.5 py-0.5 font-mono text-xs text-white"
          title={t("camera_live_fps_help")}
        >
          {measuredFps !== null ? `${formatNumber(measuredFps, { minimumFractionDigits: 1, maximumFractionDigits: 1 })} fps` : t("camera_live_fps_none")}
        </div>
      {/if}
      <LiveStream
        slug={camera.slug}
        class="block h-auto w-full select-none"
        bindVideo={(el) => { videoEl = el; }}
        onready={() => { syncImgSize(); drawDetections(); }}
      />
      <canvas
        bind:this={canvas}
        class="pointer-events-none absolute inset-0"
      ></canvas>

      {#if imgWidth > 0 && imgHeight > 0}
          <svg
            class="pointer-events-auto absolute inset-0 h-full w-full"
            style="cursor: {mode === 'draw' || mode === 'ai-click' || mode === 'circle' || mode === 'rect' ? 'crosshair' : 'default'}; touch-action: {mode === 'circle' || mode === 'rect' ? 'none' : 'auto'};"
            viewBox="0 0 {imgWidth} {imgHeight}"
            onclick={onSvgClick}
            ondblclick={onSvgDblClick}
            onpointerdown={onSvgPointerDown}
            onpointermove={onSvgPointerMove}
            onpointerup={onSvgPointerUp}
            role="presentation"
          >
            <!-- Existing zones (hideable — the fills can bury detection bboxes).
                 Click one to edit its shape; largest first so a big zone can't
                 bury a small one (see zonesByArea). -->
            {#if showZones}
              {#each zonesByArea as z (z.id)}
                {#if !(mode === "shape" && shapeTarget?.id === z.id)}
                  <polygon
                    points={pointsAttr(z.polygon)}
                    fill={z.color}
                    fill-opacity={z.enabled ? 0.22 : 0.08}
                    stroke={z.color}
                    stroke-width="2"
                    stroke-opacity={z.enabled ? 0.9 : 0.4}
                    stroke-dasharray={z.enabled ? "" : "6 4"}
                    role="button"
                    tabindex={mode === "view" ? 0 : -1}
                    aria-label="{t('zone_edit_shape')}: {z.name}"
                    style="cursor: {mode === 'view' ? 'pointer' : 'default'};
                           pointer-events: {mode === 'view' ? 'auto' : 'none'}"
                    onclick={(e) => { e.stopPropagation(); startEditZone(z); }}
                    onkeydown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        // stopPropagation is load-bearing, not hygiene: startEditZone
                        // flips mode to "shape" synchronously, and this same keydown
                        // would then reach the window listener (onMount) — which reads
                        // the NEW mode, takes the "Enter saves the shape" branch, and
                        // saves+closes the editor on the very keystroke that opened it
                        // (firing a pointless PATCH). The list's ✎ <button> is immune
                        // because there Enter's synthetic click lands after keydown
                        // propagation has already finished.
                        e.stopPropagation();
                        startEditZone(z);
                      }
                    }}
                  />
                  {#if z.polygon.length > 0}
                    <text
                      x={z.polygon[0][0] * imgWidth + 6}
                      y={z.polygon[0][1] * imgHeight + 14}
                      fill="white"
                      stroke="black"
                      stroke-width="3"
                      paint-order="stroke"
                      font-size="12"
                      font-weight="600"
                      style="pointer-events: none"
                    >{z.name}</text>
                  {/if}
                {/if}
              {/each}
            {/if}

            <!-- Bulk VLM suggestions (pending acceptance). Inert to the pointer,
                 like every other transient overlay here: they are drawn last, so
                 an SVG fill (hit-testable however transparent it is) would sit on
                 top of the saved zones and swallow the click-to-edit for any zone
                 a suggestion happens to overlap. -->
            {#each suggestions as s, i (i)}
              <polygon
                points={pointsAttr(s.polygon)}
                fill={s.color}
                fill-opacity="0.18"
                stroke={s.color}
                stroke-width="2"
                stroke-dasharray="4 4"
                style="pointer-events: none"
              />
              {#if s.polygon.length > 0}
                <text
                  x={s.polygon[0][0] * imgWidth + 6}
                  y={s.polygon[0][1] * imgHeight + 14}
                  fill="white"
                  stroke="black"
                  stroke-width="3"
                  paint-order="stroke"
                  font-size="12"
                  font-style="italic"
                  style="pointer-events: none"
                >AI: {s.name}</text>
              {/if}
            {/each}

            <!-- Shape being edited: polygon + drag/insert handles. The body is
                 grabbable to move the whole zone; the handles below are drawn
                 after it, so they take precedence in the hit-test. -->
            {#if mode === "shape" && shapePolygon.length > 0}
              {#if shapePolygon.length >= 3}
                <polygon
                  points={pointsAttr(shapePolygon)}
                  fill={zoneColor}
                  fill-opacity="0.22"
                  stroke={zoneColor}
                  stroke-width="2"
                  role="button"
                  tabindex="-1"
                  aria-label={t("zone_move")}
                  style="cursor: {dragBodyAnchor ? 'grabbing' : 'grab'}; touch-action: none"
                  onpointerdown={onPolyPointerDown}
                  onpointermove={onPolyPointerMove}
                  onpointerup={onPolyPointerUp}
                />
              {/if}
              <!-- Edge midpoints: click to insert a new vertex -->
              {#each shapePolygon as p, i}
                {@const next = shapePolygon[(i + 1) % shapePolygon.length]}
                <circle
                  role="button"
                  aria-label={t("zone_edit_shape")}
                  tabindex="-1"
                  cx={((p[0] + next[0]) / 2) * imgWidth}
                  cy={((p[1] + next[1]) / 2) * imgHeight}
                  r="5"
                  fill="white"
                  fill-opacity="0.4"
                  stroke={zoneColor}
                  stroke-width="1.5"
                  stroke-dasharray="2 2"
                  style="cursor: copy"
                  onclick={(e) => onMidpointClick(i, e)}
                  onkeydown={() => {}}
                />
              {/each}
              <!-- Vertices: drag to move, right-click to remove -->
              {#each shapePolygon as p, i}
                <circle
                  role="button"
                  aria-label={t("zone_edit_shape")}
                  tabindex="-1"
                  cx={p[0] * imgWidth}
                  cy={p[1] * imgHeight}
                  r={dragVertexIdx === i ? 8 : 6}
                  fill="white"
                  stroke={zoneColor}
                  stroke-width="2.5"
                  style="cursor: grab; touch-action: none"
                  onpointerdown={(e) => onVertexPointerDown(i, e)}
                  onpointermove={onVertexPointerMove}
                  onpointerup={onVertexPointerUp}
                  oncontextmenu={(e) => onVertexContextMenu(i, e)}
                />
              {/each}
              {#if shapePolygon.length > 0 && zoneName}
                <text
                  x={shapePolygon[0][0] * imgWidth + 10}
                  y={shapePolygon[0][1] * imgHeight - 6}
                  fill="white"
                  stroke="black"
                  stroke-width="3"
                  paint-order="stroke"
                  font-size="12"
                  font-weight="600"
                >{zoneName}</text>
              {/if}
            {/if}

            <!-- AI-click result polygon + click markers -->
            {#if mode === "ai-click"}
              {#if aiClickPolygon.length >= 3}
                <polygon
                  points={pointsAttr(aiClickPolygon)}
                  fill={zoneColor}
                  fill-opacity="0.24"
                  stroke={zoneColor}
                  stroke-width="2"
                  style="pointer-events: none"
                />
              {/if}
              {#each aiClickPositive as [x, y]}
                <circle
                  cx={x * imgWidth}
                  cy={y * imgHeight}
                  r="6"
                  fill="#10b981"
                  stroke="white"
                  stroke-width="2"
                  style="pointer-events: none"
                />
              {/each}
              {#each aiClickNegative as [x, y]}
                <circle
                  cx={x * imgWidth}
                  cy={y * imgHeight}
                  r="6"
                  fill="#ef4444"
                  stroke="white"
                  stroke-width="2"
                  style="pointer-events: none"
                />
              {/each}
            {/if}

            <!-- Live preview while dragging a circle or rectangle -->
            {#if (mode === "circle" || mode === "rect") && shapeStart && shapeCursor}
              {#if mode === "circle"}
                {@const cxPx = shapeStart[0] * imgWidth}
                {@const cyPx = shapeStart[1] * imgHeight}
                {@const exPx = shapeCursor[0] * imgWidth}
                {@const eyPx = shapeCursor[1] * imgHeight}
                {@const rPx = Math.hypot(exPx - cxPx, eyPx - cyPx)}
                <circle
                  cx={cxPx}
                  cy={cyPx}
                  r={rPx}
                  fill={zoneColor}
                  fill-opacity="0.18"
                  stroke={zoneColor}
                  stroke-width="2"
                  stroke-dasharray="6 3"
                  style="pointer-events: none"
                />
                <circle
                  cx={cxPx}
                  cy={cyPx}
                  r="3"
                  fill={zoneColor}
                  style="pointer-events: none"
                />
              {:else}
                {@const x1 = Math.min(shapeStart[0], shapeCursor[0]) * imgWidth}
                {@const x2 = Math.max(shapeStart[0], shapeCursor[0]) * imgWidth}
                {@const y1 = Math.min(shapeStart[1], shapeCursor[1]) * imgHeight}
                {@const y2 = Math.max(shapeStart[1], shapeCursor[1]) * imgHeight}
                <rect
                  x={x1}
                  y={y1}
                  width={x2 - x1}
                  height={y2 - y1}
                  fill={zoneColor}
                  fill-opacity="0.18"
                  stroke={zoneColor}
                  stroke-width="2"
                  stroke-dasharray="6 3"
                  style="pointer-events: none"
                />
              {/if}
            {/if}

            <!-- In-progress polygon. Kept on screen through "meta" (the
                 name/kind/colour form) too: finishDraw and the circle/rect
                 commit both switch to meta with drawingPts still populated, so
                 gating on "draw" alone made the shape you had just drawn vanish
                 the instant you clicked Finish — you then named it and picked a
                 colour against bare video, with the colour picker previewing
                 nothing. The sibling modes both keep their geometry up
                 (ai-click draws aiClickPolygon, shape draws shapePolygon);
                 meta was the odd one out. -->
            {#if (mode === "draw" || mode === "meta") && drawingPts.length > 0}
              {#if drawingPts.length >= 3}
                <polygon
                  points={pointsAttr(drawingPts)}
                  fill={zoneColor}
                  fill-opacity="0.2"
                  stroke={zoneColor}
                  stroke-width="2"
                />
              {:else}
                <polyline
                  points={pointsAttr(drawingPts)}
                  fill="none"
                  stroke={zoneColor}
                  stroke-width="2"
                />
              {/if}
              {#each drawingPts as [x, y]}
                <circle
                  cx={x * imgWidth}
                  cy={y * imgHeight}
                  r="4"
                  fill="white"
                  stroke={zoneColor}
                  stroke-width="2"
                />
              {/each}
            {/if}
          </svg>
        {/if}
      </div>

      <!-- Toolbar -->
      <div class="mt-3 flex flex-wrap items-center gap-3">
        {#if mode === "view"}
          <Button tone="primary" onclick={startAiClick}>✦ {t("zone_ai_click")}</Button>
          <Button tone="accent" onclick={() => suggestZonesWith()} disabled={suggesting}>{suggesting ? t("zone_ai_suggesting") : `✨ ${t("zone_ai_suggest")}`}</Button>
          <!-- Auto-tune zone rules. Only useful once zones exist. -->
          {#if zones.length > 0}
            <Button tone="accent" onclick={tuneCollecting || tuneAsking ? cancelTune : startTune} title={t("zone_tune_help")}>
              {#if tuneCollecting}
                ⏱ {formatNumber(tuneRemainingMs / 1000, { maximumFractionDigits: 0 })} s · {t("zone_tune_cancel")}
              {:else if tuneAsking}
                ✦ {t("zone_tune_asking")}
              {:else}
                ✦ {t("zone_tune_button")}
              {/if}
            </Button>
          {/if}
          {#if zones.length > 0}
            <Button onclick={toggleZones} title={t("zone_overlay_toggle_help")}>{showZones ? `◉ ${t("zone_overlay_hide")}` : `◎ ${t("zone_overlay_show")}`}</Button>
          {/if}
          <div class="relative">
            <Button onclick={() => (shapeMenuOpen = !shapeMenuOpen)}>+ {t("zone_shape_menu")} ▾</Button>
            {#if shapeMenuOpen}
              <ul
                class="absolute left-0 top-full z-10 mt-1 min-w-[12rem] overflow-hidden rounded border border-baba-border bg-baba-panel shadow-lg"
                {@attach (el: HTMLElement) => {
                  const onDocPointer = (e: PointerEvent) => {
                    if (!el.contains(e.target as Node)) shapeMenuOpen = false;
                  };
                  // Defer one tick so the same click that opened us
                  // doesn't immediately close us via this listener.
                  const id = setTimeout(() => {
                    document.addEventListener("pointerdown", onDocPointer);
                  }, 0);
                  return () => {
                    clearTimeout(id);
                    document.removeEventListener("pointerdown", onDocPointer);
                  };
                }}
              >
                <li>
                  <button
                    type="button"
                    onclick={() => { shapeMenuOpen = false; startDraw(); }}
                    class="block w-full px-3 py-1.5 text-left text-m text-baba-text hover:bg-baba-panel-2"
                  >✎ {t("zone_polygon")}</button>
                </li>
                <li>
                  <button
                    type="button"
                    onclick={() => { shapeMenuOpen = false; startCircle(); }}
                    class="block w-full px-3 py-1.5 text-left text-m text-baba-text hover:bg-baba-panel-2"
                  >◯ {t("zone_circle")}</button>
                </li>
                <li>
                  <button
                    type="button"
                    onclick={() => { shapeMenuOpen = false; startRect(); }}
                    class="block w-full px-3 py-1.5 text-left text-m text-baba-text hover:bg-baba-panel-2"
                  >▭ {t("zone_rect")}</button>
                </li>
              </ul>
            {/if}
          </div>
        {:else if mode === "draw"}
          <span class="text-m text-baba-text-muted">{t("zone_add_drawing")}</span>
          <Tag tone="quiet">
            {t("zone_drawing_points")}: {drawingPts.length}
          </Tag>
          <Button tone="primary" onclick={finishDraw} disabled={drawingPts.length < 3}>{t("zone_drawing_finish")}</Button>
          <Button onclick={cancelDraw}>{t("zone_drawing_cancel")}</Button>
        {:else if mode === "meta"}
          <span class="text-m text-baba-text-muted">{t("zone_drawing_points")}: {drawingPts.length}</span>
          <Button onclick={cancelDraw}>{t("zone_drawing_cancel")}</Button>
        {:else if mode === "shape"}
          <span class="text-m text-baba-text-muted">{t("zone_edit_shape")}: {zoneName}</span>
          <Tag tone="quiet">
            {t("zone_drawing_points")}: {shapePolygon.length}
          </Tag>
          <Button onclick={cancelShape}>{t("zone_drawing_cancel")}</Button>
        {:else if mode === "circle" || mode === "rect"}
          <span class="text-m text-baba-text-muted">
            {mode === "circle" ? t("zone_circle_hint") : t("zone_rect_hint")}
          </span>
          <Button onclick={cancelPrimitive}>{t("zone_drawing_cancel")}</Button>
        {:else if mode === "ai-click"}
          {#if aiClickBusy}
            <span class="text-m text-baba-accent">{t("zone_ai_click_segmenting")}</span>
          {:else if aiClickPolygon.length >= 3}
            <Tag tone="busy">
              ✦ {aiClickPositive.length}+ / {aiClickNegative.length}−
            </Tag>
            <Button tone="accent" onclick={aiClassifyCurrent} disabled={aiClassifyBusy}>{aiClassifyBusy ? t("zone_ai_click_classifying") : t("zone_ai_classify")}</Button>
            <Button tone="primary" onclick={saveAiClick} disabled={savingZone}>{savingZone ? t("zone_saving") : t("zone_save")}</Button>
          {:else}
            <span class="text-m text-baba-text-muted">{t("zone_ai_click_hint")}</span>
          {/if}
          <Button onclick={cancelAiClick}>{t("zone_drawing_cancel")}</Button>
        {/if}
      </div>

      {#if mode === "shape"}
        <p class="mt-2 text-s text-baba-text-muted">{t("zone_shape_hint")}</p>
      {:else if mode === "view" && showZones && zones.length > 0}
        <!-- Without this the click affordance stays invisible: the only other
             entry point is the unlabelled ✎ in the list, which is why nobody
             found editing at all. -->
        <p class="mt-2 text-s text-baba-text-muted">{t("zone_click_hint")}</p>
      {:else if mode === "ai-click"}
        <p class="mt-2 text-s text-baba-text-muted">{t("zone_ai_click_hint")}</p>
        {#if aiClickError}
          <p class="mt-1 text-s text-red-400">{aiClickError}</p>
        {/if}
      {/if}

      {#if (mode === "meta") || (mode === "shape") || (mode === "ai-click" && aiClickPolygon.length >= 3 && !aiClickBusy)}
        <div class="mt-4"><Card>
          <div class="grid grid-cols-1 gap-3 md:grid-cols-3">
            <label class="block md:col-span-2">
              <span class="block text-s text-baba-text-muted">{t("zone_name")}</span>
              <input
                bind:value={zoneName}
                class="w-full rounded border border-baba-border bg-baba-panel-2 px-2 py-1.5 text-m"
              />
            </label>
            <label class="block">
              <span class="block text-s text-baba-text-muted">{t("zone_kind")}</span>
              <select
                bind:value={zoneKind}
                class="w-full rounded border border-baba-border bg-baba-panel-2 px-2 py-1.5 text-m"
              >
                {#each KIND_KEYS as k}
                  <option value={k.value}>{t(k.key)}</option>
                {/each}
              </select>
            </label>
            <label class="flex items-center gap-2 text-m">
              <span class="text-s text-baba-text-muted">{t("zone_color")}</span>
              <input
                type="color"
                bind:value={zoneColor}
                class="h-7 w-12 cursor-pointer rounded border border-baba-border bg-transparent"
              />
            </label>
          </div>
          <div class="mt-3">
            <Button tone="primary" onclick={
                mode === "shape" ? saveShape
                : mode === "ai-click" ? saveAiClick
                : saveDrawn
              } disabled={mode === "shape" ? (savingShape || shapePolygon.length < 3) : savingZone}>{(mode === "shape" ? savingShape : savingZone) ? t("zone_saving") : t("zone_save")}</Button>
          </div>
        </Card></div>
      {/if}

    <!-- Zone list + per-zone rules + AI suggestions (full-width, below stream) -->
    <div class="space-y-4">
      <Card title={t("zone_list_title")}>
        {#if zones.length === 0}
          <p class="text-s text-baba-text-faint">{t("zone_list_empty")}</p>
        {:else}
          <ul class="space-y-2">
            {#each zones as z (z.id)}
              <li class="flex items-center gap-2 rounded border border-baba-border bg-baba-panel-2 p-2">
                <span class="inline-block h-4 w-4 shrink-0 rounded" style="background:{z.color}"></span>
                <div class="min-w-0 flex-1">
                  <div class="truncate text-m font-medium">{z.name}</div>
                  <div class="text-s text-baba-text-faint">
                    {kindLabel(z.kind)} · {z.enabled ? t("zone_enabled") : t("zone_disabled")}
                  </div>
                </div>
                <Button size="small" title={t("zone_edit_shape")} label={t("zone_edit_shape")} disabled={mode !== "view"} onclick={() => startEditZone(z)}>✎</Button>
                <Button size="small" selected={rulesZoneId === z.id} title={t("detection_rules_zone_title")} label={t("detection_rules_zone_title")} onclick={() => rulesZoneId = rulesZoneId === z.id ? null : z.id}>⚙</Button>
                <Button size="small" title={z.enabled ? t("zone_disabled") : t("zone_enabled")} onclick={() => toggleZoneEnabled(z)}>{z.enabled ? "●" : "○"}</Button>
                <Button tone="danger" size="small" onclick={() => deleteZone(z)} label={t("dialog_delete")}>✕</Button>
              </li>
            {/each}
          </ul>
        {/if}
      </Card>

      {#if rulesZone}
        <ZoneRulesCard
          zone={rulesZone}
          onUpdate={(updated) => {
            zones = zones.map((z) => (z.id === updated.id ? updated : z));
            // Saved state caught up with the preview — drop the override.
            rulesZonePreview = null;
          }}
          onPreview={(preview) => { rulesZonePreview = preview; }}
        />
      {/if}

      {#if tuneProposals.length > 0 || tuneError}
        <section class="rounded-lg border border-emerald-500/40 bg-emerald-500/5 p-3">
          <div class="mb-2 flex items-center justify-between">
            <h3 class="text-m font-medium text-emerald-300">
              ✦ {t("zone_tune_title")}
            </h3>
            {#if tuneProposals.length > 0}
              <Button size="small" onclick={() => { tuneProposals = []; }}>{t("zone_ai_dismiss_all")}</Button>
            {/if}
          </div>
          {#if tuneError}
            <p class="text-s text-red-400">{tuneError}</p>
          {/if}
          <ul class="space-y-2">
            {#each tuneProposals as p (p.zone_id)}
              {@const currentZone = zones.find((z) => z.id === p.zone_id)}
              <li class="rounded border border-baba-border bg-baba-panel-2 p-3 text-m">
                <div class="mb-2 flex items-center justify-between gap-2">
                  <div class="flex items-center gap-2">
                    {#if currentZone}
                      <span class="inline-block h-4 w-4 shrink-0 rounded" style="background:{currentZone.color}"></span>
                    {/if}
                    <span class="font-medium">{p.zone_name}</span>
                  </div>
                  <div class="flex gap-1">
                    <Button tone="primary" size="small" onclick={() => applyProposal(p)}>{t("zone_tune_apply")}</Button>
                    <Button size="small" onclick={() => dismissProposal(p)} label={t("zone_ai_dismiss")}>✕</Button>
                  </div>
                </div>
                {#if p.rationale}
                  <p class="mb-2 text-s text-baba-text-muted">{p.rationale}</p>
                {/if}
                <div class="grid grid-cols-1 gap-1 text-s sm:grid-cols-2">
                  {#each Object.entries(p.enabled_classes) as [cls, rule] (cls)}
                    {@const current = currentZone?.rules?.enabled_classes?.[cls]}
                    {@const curConf = current?.min_confidence}
                    <div class="flex items-center gap-2 rounded border border-baba-border bg-baba-panel px-2 py-1.5">
                      <span class="font-mono">{classLabel(cls)}</span>
                      <span class="ml-auto font-mono text-baba-text-muted">
                        {#if curConf != null}
                          {formatNumber(curConf, { minimumFractionDigits: 2, maximumFractionDigits: 2 })} → <span class="font-medium text-emerald-300">{formatNumber(rule.min_confidence ?? 0, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span>
                        {:else}
                          {t("zone_tune_new")} → <span class="font-medium text-emerald-300">{formatNumber(rule.min_confidence ?? 0, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</span>
                        {/if}
                      </span>
                    </div>
                  {/each}
                </div>
              </li>
            {/each}
          </ul>
        </section>
      {/if}

      {#if suggestions.length > 0 || suggestError}
        <section class="rounded-lg border border-baba-accent/40 bg-baba-accent/5 p-3">
          <div class="mb-2 flex items-center justify-between">
            <h3 class="text-m font-medium text-baba-accent">
              ✨ {t("zone_ai_suggestions_title")}
            </h3>
            {#if suggestions.length > 0}
              <Button size="small" onclick={dismissAllSuggestions}>{t("zone_ai_dismiss_all")}</Button>
            {/if}
          </div>
          {#if suggestError}
            <p class="text-s text-red-400">{suggestError}</p>
          {/if}
          <ul class="space-y-2">
            {#each suggestions as s, i (i)}
              <li class="rounded border border-baba-border bg-baba-panel-2 p-2 text-m">
                <div class="flex items-center gap-2">
                  <span class="inline-block h-4 w-4 shrink-0 rounded" style="background:{s.color}"></span>
                  <div class="min-w-0 flex-1">
                    <div class="truncate font-medium">{s.name}</div>
                    <div class="text-s text-baba-text-faint">{kindLabel(s.kind)}</div>
                  </div>
                  <Button tone="primary" size="small" onclick={() => acceptSuggestion(i)}>{t("zone_ai_accept")}</Button>
                  <Button size="small" onclick={() => dismissSuggestion(i)} label={t("zone_ai_dismiss")}>✕</Button>
                </div>
                {#if s.rationale}
                  <p class="mt-1.5 text-s text-baba-text-muted">
                    <span class="text-baba-text-faint">{t("zone_ai_rationale")}:</span> {s.rationale}
                  </p>
                {/if}
              </li>
            {/each}
          </ul>
        </section>
      {/if}
    </div>
  </div>
{/if}
