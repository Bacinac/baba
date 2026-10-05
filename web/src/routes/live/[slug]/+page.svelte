<script lang="ts">
  import { formatNumber, plural } from "$lib/kit";
  import { t } from "$lib/i18n";
  import { onDestroy, onMount } from "svelte";
  import { page } from "$app/state";
  import {
    api,
    detectionsFeed,
    tracksFeed,
    type Camera,
    type DetectionsMessage,
    type TracksMessage,
    type Zone,
    type Feed,
  } from "$lib/api";
  import LiveStream from "$lib/LiveStream.svelte";

  let slug = $derived(page.params.slug as string);
  let camera = $state<Camera | null>(null);
  // Only for the ignore-zone half of the dead-end test. Without them this
  // page would grey a suppressed phantom but still paint a detection the
  // operator has explicitly zoned out as a live one — the same
  // inconsistency, one reason over.
  let zones = $state<Zone[]>([]);
  let loadError = $state<string | null>(null);
  let lastMessage = $state<DetectionsMessage | null>(null);
  // Parallel tracks feed: carries the class-STABILIZED label + motion_state
  // per object, so the live overlay shows "car · PARKED 2m" instead of the
  // raw per-frame class (which flips to motorcycle/dog on odd viewpoints).
  let lastTracksMsg = $state<TracksMessage | null>(null);
  // Measured detection frame rate (rolling, from message arrivals) — with the
  // adaptive per-camera rate the static target_fps doesn't describe the
  // current moment; this does.
  let measuredFps = $state<number | null>(null);
  let fpsArrivals: number[] = [];
  function noteArrival() {
    const now = performance.now();
    fpsArrivals.push(now);
    if (fpsArrivals.length > 12) fpsArrivals.shift();
    if (fpsArrivals.length >= 3) {
      const spanMs = now - fpsArrivals[0];
      if (spanMs > 0) measuredFps = ((fpsArrivals.length - 1) * 1000) / spanMs;
    }
  }

  let canvas: HTMLCanvasElement | null = $state(null);
  let videoBox: HTMLDivElement | null = $state(null);
  let videoEl: HTMLVideoElement | null = $state(null);
  let streamError = $state<string | null>(null);
  let es: Feed | null = null;
  let esTracks: Feed | null = null;

  import { classColor, MOTION_COLORS, motionStateLabel } from "$lib/detectionColors";
  import {
    DEAD_END_ALPHA,
    DEAD_END_COLOR,
    DEAD_END_DASH,
    MAINTAIN_ALPHA,
    MAINTAIN_DASH,
    deadEnd,
    deadEndLabel,
    maintainLabel,
  } from "$lib/detectionState";
  import { liveOverlay } from "$lib/liveOverlay.svelte";

  function draw() {
    if (!canvas || !videoBox || !videoEl || !lastMessage) return;
    if (videoEl.videoWidth === 0) return;

    const boxRect = videoBox.getBoundingClientRect();
    canvas.width = boxRect.width;
    canvas.height = boxRect.height;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    // Global overlay toggle — a cleared canvas over the running MSE video.
    if (!liveOverlay.boxes) return;

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

    // Label anti-collision — adjacent boxes put their labels on the same
    // row; step a colliding label down a line until it finds air.
    const placedLabels: { x: number; y: number; w: number }[] = [];
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
      placedLabels.push({ x: lx, y: yTry, w: lw });
      return yTry;
    };

    // Match a raw detection to its track (class-stabilized label + motion
    // state). Same policy as the ZoneEditor overlay: prefer a same-class
    // match, accept a cross-class one at stricter IoU — the raw class can
    // flip arbitrarily (car→dog) while the track stays put.
    const trackerBoxes = lastTracksMsg?.tracks ?? [];
    const findTrack = (det: { x1: number; y1: number; x2: number; y2: number; class_id: number }) => {
      let bestSame = null as (typeof trackerBoxes)[number] | null;
      let bestSameIou = 0;
      let bestAny = null as (typeof trackerBoxes)[number] | null;
      let bestAnyIou = 0;
      for (const tr of trackerBoxes) {
        const xx1 = Math.max(det.x1, tr.x1);
        const yy1 = Math.max(det.y1, tr.y1);
        const xx2 = Math.min(det.x2, tr.x2);
        const yy2 = Math.min(det.y2, tr.y2);
        const inter = Math.max(0, xx2 - xx1) * Math.max(0, yy2 - yy1);
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
      if (bestSame !== null && bestSameIou > 0.3) return bestSame;
      if (bestAny !== null && bestAnyIou > 0.45) return bestAny;
      return null;
    };

    for (const det of lastMessage.detections) {
      const tr = findTrack(det);
      let color = classColor(tr ? tr.class_id : det.class_id);
      let dash: number[] = [];
      let motionLabel = "";
      let alpha = 1;
      // This view draws the RAW detector feed, so it also carries boxes the
      // pipeline discards. Marking them is not cosmetic: an unmarked
      // suppressed bench reads as a confident "truck 66%" on the monitoring
      // page while the tracker has ignored that spot for a week.
      const dead = deadEnd(det, zones, lastMessage.frame_width, lastMessage.frame_height);
      if (dead) {
        color = DEAD_END_COLOR;
        dash = DEAD_END_DASH;
        alpha = DEAD_END_ALPHA;
        motionLabel = deadEndLabel(dead);
      } else if (tr && tr.motion_state !== "active") {
        const ageSec = tr.state_since_ns
          ? Math.max(0, (Date.now() * 1e6 - tr.state_since_ns) / 1e9)
          : 0;
        const ageStr =
          ageSec >= 60
            ? `${Math.floor(ageSec / 60)}m ${Math.floor(ageSec % 60)}s`
            : `${formatNumber(ageSec, { maximumFractionDigits: 0 })}s`;
        motionLabel = ` · ${motionStateLabel(tr.motion_state, tr.class_id)} ${ageStr}`;
        color = tr.motion_state === "parked" ? MOTION_COLORS.parked : MOTION_COLORS.stationary;
        dash = [4, 3];
      }
      if (!dead && det.birth_eligible === false) {
        alpha = MAINTAIN_ALPHA;
        if (dash.length === 0) dash = MAINTAIN_DASH;
        motionLabel += maintainLabel();
      }
      const x = contentX + det.x1 * sx;
      const y = contentY + det.y1 * sy;
      const w = (det.x2 - det.x1) * sx;
      const h = (det.y2 - det.y1) * sy;
      // Dark halo under the coloured stroke + outlined label — same
      // high-contrast treatment as the ZoneEditor preview, readable over
      // bright daylight video where a bare 2px pastel line washed out.
      ctx.globalAlpha = alpha;
      ctx.setLineDash(dash);
      ctx.strokeStyle = "rgba(0,0,0,0.85)";
      ctx.lineWidth = 4.5;
      ctx.strokeRect(x, y, w, h);
      ctx.strokeStyle = color;
      ctx.lineWidth = 2.5;
      ctx.strokeRect(x, y, w, h);
      ctx.setLineDash([]);
      const label = `${tr ? tr.class_name : det.class_name} ${Math.round(det.confidence * 100)}%${motionLabel}`;
      ctx.font = "600 13px ui-sans-serif, system-ui, sans-serif";
      const labelY = placeLabel(x + 4, y + 15, ctx.measureText(label).width);
      ctx.lineJoin = "round";
      ctx.lineWidth = 3;
      ctx.strokeStyle = "rgba(0,0,0,0.9)";
      ctx.strokeText(label, x + 4, labelY);
      ctx.fillStyle = color;
      ctx.fillText(label, x + 4, labelY);
      ctx.globalAlpha = 1;
    }
  }

  $effect(() => {
    void lastTracksMsg;
    if (lastMessage) draw();
  });

  // React to the :slug param: (re)fetch the camera and (re)connect the
  // detections SSE. Navigating /live/cam1 → /live/cam2 keeps neither stale
  // (the old effect run's cleanup closes the previous stream), and an unknown
  // slug surfaces a friendly error instead of throwing on a `!` assertion.
  liveOverlay.load(); // global boxes toggle; draw() reacts once it resolves

  $effect(() => {
    const s = slug;
    camera = null;
    loadError = null;
    lastMessage = null;
    lastTracksMsg = null;
    zones = [];
    measuredFps = null;
    fpsArrivals = [];
    let cancelled = false;
    let localEs: Feed | null = null;
    let localEsTracks: Feed | null = null;
    (async () => {
      try {
        const match = (await api.listCameras()).find((c) => c.slug === s);
        if (cancelled) return;
        if (!match) {
          loadError = t("camera_not_found");
          return;
        }
        const full = await api.getCamera(match.id);
        if (cancelled) return;
        camera = full;
        try {
          const z = await api.listZones(match.id);
          if (!cancelled) zones = z;
        } catch {
          // A missing zone list only costs the ignore-zone marking; the
          // phantom one rides on the wire and still works.
        }
      } catch (e) {
        if (!cancelled) loadError = (e as Error).message;
        return;
      }
      if (cancelled) return;
      localEs = detectionsFeed(s, {
        message: (m) => {
          if (cancelled) return;
          lastMessage = m;
          noteArrival();
        },
      });
      es = localEs;
      localEsTracks = tracksFeed(s, {
        message: (m) => { if (!cancelled) lastTracksMsg = m; },
      });
      esTracks = localEsTracks;
    })();
    return () => {
      cancelled = true;
      if (localEs) localEs.close();
      if (es === localEs) es = null;
      if (localEsTracks) localEsTracks.close();
      if (esTracks === localEsTracks) esTracks = null;
    };
  });

  // Re-observe reactively: on a slug change the video element is recreated,
  // so an onMount-once observer would keep watching the stale node. $effect
  // re-runs when videoBox changes, disconnecting the old and observing the new.
  $effect(() => {
    if (!videoBox) return;
    const ro = new ResizeObserver(draw);
    ro.observe(videoBox);
    return () => ro.disconnect();
  });

  onDestroy(() => {
    if (es) es.close();
    if (esTracks) esTracks.close();
  });
</script>

<div class="mb-4 flex items-center justify-between">
  <div>
    <a href="/live" class="text-s text-baba-text-faint hover:text-baba-text-muted">{t("camera_back_to_live")}</a>
    <h2 class="text-2xl font-semibold">{camera?.name ?? slug}</h2>
    <p class="text-s text-baba-text-faint">
      {measuredFps !== null ? formatNumber(measuredFps, { minimumFractionDigits: 1, maximumFractionDigits: 1 }) : (camera?.target_fps ?? "?")} {t("cameras_fps_suffix")}
      {#if lastMessage}· {lastMessage.frame_width}×{lastMessage.frame_height}{/if}
    </p>
  </div>
  {#if lastMessage}
    <div class="text-right text-s text-baba-text-muted">
      <div>{t("camera_frame")} #{lastMessage.sequence}</div>
      <div>{lastMessage.detections.length} {plural(lastMessage.detections.length, "camera_detections_one", "camera_detections_few", "camera_detections_many")}</div>
    </div>
  {/if}
</div>

{#if loadError}
  <div class="mb-4 rounded border border-red-500/40 bg-red-500/10 px-3 py-2 text-m text-red-300">
    {loadError}
  </div>
{/if}

<div bind:this={videoBox} class="relative w-full max-w-5xl overflow-hidden rounded-lg bg-black">
  <LiveStream
    {slug}
    class="block h-auto w-full"
    bindVideo={(el) => (videoEl = el)}
    onready={draw}
    onstate={(s, d) => (streamError = s === "error" ? (d ?? t("live_stream_unavailable")) : null)}
  />
  <canvas bind:this={canvas} class="pointer-events-none absolute inset-0"></canvas>
  {#if streamError}
    <div
      class="pointer-events-none absolute inset-0 flex min-h-40 flex-col items-center justify-center gap-1 bg-black/60 text-center"
    >
      <span class="text-m font-medium text-red-300">{t("live_stream_unavailable")}</span>
      <span class="text-s text-baba-text-faint">{streamError}</span>
    </div>
  {/if}
</div>
