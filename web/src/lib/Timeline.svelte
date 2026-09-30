<script lang="ts">
  // Scrubable recording timeline over an arbitrary [windowStartMs, windowEndMs]
  // window. Renders one lane per camera with segments drawn as rectangles and
  // events as small markers below the lane. SVG keeps event hit-testing simple
  // (each marker is its own DOM node) and scales cleanly with container width.
  // X maps linearly across the window; axis ticks adapt to the span. A
  // playhead-centered 10-min detail strip gives smooth fine scrubbing even
  // when the overview window is wide.

  import type { BabaEvent, Camera, Recording } from "$lib/api";
  import { t } from "$lib/i18n";
  import { dt } from "$lib/datetime.svelte";
  // Shared neon palette — a class reads as the same hue here as on the
  // live/zone-editor detection overlays.
  import { classColor as colorForClass } from "$lib/detectionColors";

  type Props = {
    // Time window the overview spans, in absolute ms. Any window works
    // (1h, 24h, a calendar day, 7d) — the axis ticks adapt to the span.
    windowStartMs: number;
    windowEndMs: number;
    cameras: Camera[];
    recordings: Recording[];
    events: BabaEvent[];
    /** Camera id of the playing source; null = nothing selected yet. */
    activeCameraId: string | null;
    /** Absolute time in ms shown by the playhead (null = hidden). */
    playheadMs: number | null;
    onPickRecording: (r: Recording, seekSec: number) => void;
    onPickEvent: (ev: BabaEvent) => void;
    /** Scrub to an absolute ms time. Parent decides whether to fine-seek
     *  the active <video> in-place or pick a different segment. */
    onSeekTo: (absMs: number) => void;
  };
  let {
    windowStartMs,
    windowEndMs,
    cameras,
    recordings,
    events,
    activeCameraId,
    playheadMs,
    onPickRecording,
    onPickEvent,
    onSeekTo,
  }: Props = $props();

  // Window span (>=1 to avoid divide-by-zero).
  let windowMs = $derived(Math.max(1, windowEndMs - windowStartMs));

  // Layout constants. The SVG uses a viewBox so it scales to container width
  // without recalculating on resize.
  const VB_WIDTH = 1440;        // 1 px = 1 minute, friendly arithmetic
  const AXIS_HEIGHT = 20;
  const LANE_HEIGHT = 22;       // compact lanes so many cameras stay scannable
  const LANE_GAP = 3;
  const GUTTER_PX = 92;         // left column for crisp (non-stretched) names

  let height = $derived(AXIS_HEIGHT + cameras.length * (LANE_HEIGHT + LANE_GAP));

  function msToX(ms: number): number {
    return ((ms - windowStartMs) / windowMs) * VB_WIDTH;
  }
  function laneY(idx: number): number {
    return AXIS_HEIGHT + idx * (LANE_HEIGHT + LANE_GAP);
  }

  // Adaptive axis ticks: pick a "nice" interval so the overview shows
  // ~8-16 gridlines regardless of span (15min for a tight hour view up to
  // 1 day for a week). Labels are clock time for sub-day intervals, date for
  // day+ intervals.
  const _TICK_STEPS_MS = [
    15 * 60_000, 30 * 60_000, 60 * 60_000, 2 * 3_600_000, 3 * 3_600_000,
    6 * 3_600_000, 12 * 3_600_000, 86_400_000, 2 * 86_400_000, 7 * 86_400_000,
  ];
  let axisTicks = $derived.by<{ x: number; label: string; major: boolean }[]>(() => {
    const target = 12;
    let step = _TICK_STEPS_MS[_TICK_STEPS_MS.length - 1];
    for (const s of _TICK_STEPS_MS) {
      if (windowMs / s <= target) { step = s; break; }
    }
    const dayStep = step >= 86_400_000;
    const out: { x: number; label: string; major: boolean }[] = [];
    if (dayStep) {
      // Day-mode ticks land on LOCAL midnight, incremented with Date day
      // arithmetic so DST transitions don't drift them off midnight.
      const days = Math.round(step / 86_400_000);
      const d = new Date(windowStartMs);
      d.setHours(0, 0, 0, 0);
      while (d.getTime() < windowStartMs) d.setDate(d.getDate() + days);
      for (; d.getTime() <= windowEndMs; d.setDate(d.getDate() + days)) {
        const t = d.getTime();
        out.push({
          x: msToX(t),
          label: dt.day(t),
          major: true,
        });
      }
    } else {
      // Sub-day ticks align to LOCAL step boundaries. Aligning to UTC-epoch
      // multiples (as before) put them at 01/07/13/19 local in CET/CEST, so
      // the `getHours() % 6 === 0` major-gridline test never fired.
      const tzOff = new Date(windowStartMs).getTimezoneOffset() * 60_000;
      const first = Math.ceil((windowStartMs - tzOff) / step) * step + tzOff;
      for (let t = first; t <= windowEndMs; t += step) {
        const d = new Date(t);
        const label = dt.hm(t);
        const major = d.getHours() % 6 === 0 && d.getMinutes() === 0;
        out.push({ x: msToX(t), label, major });
      }
    }
    return out;
  });

  function clampSegmentForWindow(r: Recording): { x: number; width: number } | null {
    const start = new Date(r.started_at).getTime();
    // Use ended_at if known, otherwise treat the segment as ongoing up to
    // (now or window-end) — whichever is sooner.
    const endRaw = r.ended_at
      ? new Date(r.ended_at).getTime()
      : Math.min(Date.now(), windowEndMs);
    const lo = Math.max(start, windowStartMs);
    const hi = Math.min(endRaw, windowEndMs);
    if (hi <= lo) return null;
    const x = msToX(lo);
    const width = Math.max(2, msToX(hi) - x);  // floor width so 1-second segments still register a click
    return { x, width };
  }

  // Index cameras by id for lane lookups + name display.
  let camIndex = $derived<Map<string, { name: string; lane: number }>>(
    new Map(cameras.map((c, i) => [c.id, { name: c.name, lane: i }])),
  );

  // How loudly a coverage bar is drawn. "playing" is the camera on screen,
  // "other" is a camera we are deliberately pushing back so the played one
  // stands out — and "idle" is every camera when NOTHING is playing.
  //
  // That third tone is the point: dimming used to key off `active` alone, so
  // closing the player made every bar the pushed-back grey and the whole
  // timeline read as empty — no footage, nothing to click, under a caption
  // inviting you to click it. There is nothing to contrast against with the
  // player closed, so coverage goes back to full readable weight.
  type Tone = "playing" | "other" | "idle";
  const toneFor = (cameraId: string): Tone =>
    activeCameraId === null ? "idle" : cameraId === activeCameraId ? "playing" : "other";

  // Pre-compute renderable segment + event rows so the template stays clean.
  type SegmentRow = { rec: Recording; lane: number; x: number; width: number; tone: Tone };
  let segmentRows = $derived.by<SegmentRow[]>(() => {
    const out: SegmentRow[] = [];
    for (const r of recordings) {
      const info = camIndex.get(r.camera.id);
      if (!info) continue;
      const dims = clampSegmentForWindow(r);
      if (!dims) continue;
      out.push({
        rec: r, lane: info.lane,
        x: dims.x, width: dims.width,
        tone: toneFor(r.camera.id),
      });
    }
    return out;
  });

  // Markers are drawn as BARS spanning the visit's duration (a sighting that
  // lasts 9 min is a 9-min bar, not a dot), inset inside the lane so the gray
  // recording-coverage shows as a thin frame around them.
  type EventBar = { ev: BabaEvent; x: number; y: number; width: number; color: string };
  let eventBars = $derived.by<EventBar[]>(() => {
    const out: EventBar[] = [];
    for (const ev of events) {
      const info = camIndex.get(ev.camera.id);
      if (!info) continue;
      const start = new Date(ev.at).getTime();
      const end = start + Math.max(0, (ev.track?.duration_s ?? 0) * 1000);
      const lo = Math.max(start, windowStartMs);
      const hi = Math.min(Math.max(end, start + 1), windowEndMs);
      if (hi <= lo) continue;
      const x = msToX(lo);
      const width = Math.max(2, msToX(hi) - x);
      out.push({
        ev, x, y: laneY(info.lane) + 2, width,
        color: colorForClass(ev.track?.class_id ?? 0),
      });
    }
    return out;
  });

  let playheadX = $derived.by<number | null>(() => {
    if (playheadMs === null) return null;
    if (playheadMs < windowStartMs || playheadMs > windowEndMs) return null;
    return msToX(playheadMs);
  });

  // --- detail strip ---
  // The 24h overview makes ~0.02 px/sec — playhead motion is invisible in
  // real time. This compact strip zooms in to a window centered on the
  // playhead so the user actually sees smooth scrubbing: the world (segments
  // + events) slides right→left under a fixed centered playhead.
  const STRIP_WINDOW_MS = 10 * 60 * 1000;   // 10 minutes
  const STRIP_VB_WIDTH = 1200;
  const STRIP_HEIGHT = 36;
  const STRIP_CENTER_X = STRIP_VB_WIDTH / 2;

  let stripWindowStart = $derived.by<number | null>(() => {
    if (playheadMs === null) return null;
    return playheadMs - STRIP_WINDOW_MS / 2;
  });

  function stripMsToX(ms: number, start: number): number {
    return ((ms - start) / STRIP_WINDOW_MS) * STRIP_VB_WIDTH;
  }

  type StripSegment = { rec: Recording; x: number; width: number; tone: Tone };
  let stripSegments = $derived.by<StripSegment[]>(() => {
    if (stripWindowStart === null) return [];
    const start = stripWindowStart;
    const end = start + STRIP_WINDOW_MS;
    const out: StripSegment[] = [];
    for (const r of recordings) {
      const segStart = new Date(r.started_at).getTime();
      const segEnd = r.ended_at ? new Date(r.ended_at).getTime() : Date.now();
      const lo = Math.max(segStart, start);
      const hi = Math.min(segEnd, end);
      if (hi <= lo) continue;
      const x = stripMsToX(lo, start);
      const width = Math.max(2, stripMsToX(hi, start) - x);
      out.push({
        rec: r,
        x,
        width,
        tone: toneFor(r.camera.id),
      });
    }
    return out;
  });

  type StripEvent = { ev: BabaEvent; x: number; width: number; color: string };
  let stripEvents = $derived.by<StripEvent[]>(() => {
    if (stripWindowStart === null) return [];
    const start = stripWindowStart;
    const end = start + STRIP_WINDOW_MS;
    const out: StripEvent[] = [];
    for (const ev of events) {
      const evStart = new Date(ev.at).getTime();
      const evEnd = evStart + Math.max(0, (ev.track?.duration_s ?? 0) * 1000);
      const lo = Math.max(evStart, start);
      const hi = Math.min(Math.max(evEnd, evStart + 1), end);
      if (hi <= lo) continue;
      const x = stripMsToX(lo, start);
      out.push({
        ev,
        x,
        width: Math.max(2, stripMsToX(hi, start) - x),
        color: colorForClass(ev.track?.class_id ?? 0),
      });
    }
    return out;
  });

  // Minute ticks: where on the strip does each whole-minute fall?
  let stripMinuteTicks = $derived.by<{ x: number; label: string }[]>(() => {
    if (stripWindowStart === null) return [];
    const start = stripWindowStart;
    const end = start + STRIP_WINDOW_MS;
    const firstMinute = Math.ceil(start / 60_000) * 60_000;
    const out: { x: number; label: string }[] = [];
    for (let t = firstMinute; t <= end; t += 60_000) {
      out.push({
        x: stripMsToX(t, start),
        label: dt.hm(t),
      });
    }
    return out;
  });

  function onSegmentClick(row: SegmentRow, mouseX: number, vbWidth: number) {
    // Translate the clientX into the SVG's viewBox space and from there to
    // seconds-into-segment.
    const xInVb = (mouseX / vbWidth) * VB_WIDTH;
    const tMs = windowStartMs + (xInVb / VB_WIDTH) * windowMs;
    const seekSec = Math.max(0, (tMs - new Date(row.rec.started_at).getTime()) / 1000);
    onPickRecording(row.rec, seekSec);
  }

  // Keyboard activation for SVG "buttons" (rects + circles). Picks at the
  // segment's visible start since there's no mouse X.
  function onSegmentKey(e: KeyboardEvent, row: SegmentRow): void {
    if (e.key !== "Enter" && e.key !== " ") return;
    e.preventDefault();
    const segStartMs = Math.max(new Date(row.rec.started_at).getTime(), windowStartMs);
    const seekSec = Math.max(0, (segStartMs - new Date(row.rec.started_at).getTime()) / 1000);
    onPickRecording(row.rec, seekSec);
  }
  function onEventKey(e: KeyboardEvent, ev: BabaEvent): void {
    if (e.key !== "Enter" && e.key !== " ") return;
    e.preventDefault();
    onPickEvent(ev);
  }

  // --- strip interaction ---
  // All input paths (click, pointer-drag, wheel, arrow keys) translate
  // their input into an absolute ms time and call `onSeekTo`. The parent
  // decides whether to fine-seek the active <video> in place (cheap) or
  // switch to a different segment (#key recreate).
  let stripDragging = $state(false);
  let stripDragMoved = false;
  let stripDragStartClientX = 0;
  let stripDragStartMs = 0;

  function stripClientXToMs(clientX: number, target: SVGSVGElement): number | null {
    if (stripWindowStart === null) return null;
    const rect = target.getBoundingClientRect();
    const xInVb = ((clientX - rect.left) / rect.width) * STRIP_VB_WIDTH;
    return stripWindowStart + (xInVb / STRIP_VB_WIDTH) * STRIP_WINDOW_MS;
  }

  function onStripPointerDown(e: PointerEvent): void {
    if (playheadMs === null) return;
    stripDragging = true;
    stripDragMoved = false;
    stripDragStartClientX = e.clientX;
    stripDragStartMs = playheadMs;
    (e.currentTarget as SVGSVGElement).setPointerCapture(e.pointerId);
  }

  function onStripPointerMove(e: PointerEvent): void {
    if (!stripDragging) return;
    const svg = e.currentTarget as SVGSVGElement;
    const dx = e.clientX - stripDragStartClientX;
    if (Math.abs(dx) >= 3) stripDragMoved = true;
    // Drag-right = move world right = look backwards in time.
    const dtMs = -(dx / svg.getBoundingClientRect().width) * STRIP_WINDOW_MS;
    onSeekTo(stripDragStartMs + dtMs);
  }

  function onStripPointerUp(e: PointerEvent): void {
    if (!stripDragging) return;
    stripDragging = false;
    (e.currentTarget as SVGSVGElement).releasePointerCapture(e.pointerId);
  }

  function onStripClick(e: MouseEvent): void {
    // Drag gestures also fire `click` on pointerup if total movement was
    // small enough — suppress when we know the user actually dragged.
    if (stripDragMoved) { stripDragMoved = false; return; }
    const target = e.currentTarget as SVGSVGElement;
    const tMs = stripClientXToMs(e.clientX, target);
    if (tMs !== null) onSeekTo(tMs);
  }

  function onStripWheel(e: WheelEvent): void {
    if (playheadMs === null) return;
    e.preventDefault();
    // 1 second per detent in either direction. Shift = 10 s for fast travel.
    const step = (e.shiftKey ? 10_000 : 1_000) * Math.sign(e.deltaY);
    if (step !== 0) onSeekTo(playheadMs + step);
  }

  function onStripKey(e: KeyboardEvent): void {
    if (playheadMs === null) return;
    let delta = 0;
    if (e.key === "ArrowLeft") delta = e.shiftKey ? -10_000 : -1_000;
    else if (e.key === "ArrowRight") delta = e.shiftKey ? 10_000 : 1_000;
    else return;
    e.preventDefault();
    onSeekTo(playheadMs + delta);
  }
</script>

<div class="overflow-hidden rounded-lg border border-baba-border bg-baba-panel">
 <div class="flex">
  <!-- Left gutter: camera names as crisp HTML (the SVG uses
       preserveAspectRatio=none which would horizontally squash SVG text).
       Rows line up 1:1 with the SVG lanes (y is unscaled). -->
  <div class="shrink-0 border-r border-baba-border bg-baba-panel-2" style:width="{GUTTER_PX}px">
    <div style:height="{AXIS_HEIGHT}px"></div>
    {#each cameras as cam (cam.id)}
      <div
        class="flex items-center truncate px-2 text-s font-medium text-baba-text"
        style:height="{LANE_HEIGHT + LANE_GAP}px"
        title={cam.name}
      >{cam.name}</div>
    {/each}
  </div>

  <svg
    viewBox="0 0 {VB_WIDTH} {height}"
    preserveAspectRatio="none"
    class="block min-w-0 flex-1"
    style:height="{height}px"
    role="img"
    aria-label={t("recordings_title")}
  >
    <!-- adaptive time grid -->
    {#each axisTicks as tick (tick.x)}
      <line
        x1={tick.x} y1={0}
        x2={tick.x} y2={height}
        stroke="var(--color-baba-border)"
        stroke-width={tick.major ? 1.5 : 0.5}
        vector-effect="non-scaling-stroke"
      />
      <text
        x={tick.x + 4}
        y={AXIS_HEIGHT - 6}
        class="fill-baba-text-faint"
        style="font: 10px ui-sans-serif, system-ui, sans-serif;"
      >{tick.label}</text>
    {/each}

    <!-- lane backgrounds (names live in the HTML gutter on the left) -->
    {#each cameras as cam, i (cam.id)}
      <rect
        x={0} y={laneY(i)} width={VB_WIDTH} height={LANE_HEIGHT}
        fill="var(--color-baba-panel-2)"
      />
    {/each}

    <!-- recording coverage (subtle background — footage exists here, scrubable;
         the colored sighting bars on top are the activity). -->
    {#each segmentRows as row (row.rec.id)}
      {@const top = laneY(row.lane) + 2}
      {@const h = LANE_HEIGHT - 4}
      <rect
        x={row.x} y={top}
        width={row.width} height={h}
        fill={row.tone === "playing" ? "var(--color-baba-accent)" : "var(--color-baba-text-muted)"}
        fill-opacity={row.tone === "other" ? 0.1 : 0.22}
        stroke={row.tone === "playing" ? "var(--color-baba-accent)" : "transparent"}
        stroke-width="1"
        vector-effect="non-scaling-stroke"
        class="cursor-pointer transition-opacity hover:fill-opacity-30"
        onclick={(e) => {
          const svg = (e.currentTarget as SVGElement).ownerSVGElement!;
          const rect = svg.getBoundingClientRect();
          onSegmentClick(row, (e as MouseEvent).clientX - rect.left, rect.width);
        }}
        onkeydown={(e) => onSegmentKey(e, row)}
        role="button"
        tabindex="0"
      >
        <title>{row.rec.camera.name} — {dt.hms(row.rec.started_at)}</title>
      </rect>
    {/each}

    <!-- sighting bars (span the visit duration) -->
    {#each eventBars as bar (bar.ev.id)}
      <rect
        x={bar.x} y={bar.y}
        width={bar.width} height={LANE_HEIGHT - 4}
        rx="1.5"
        fill={bar.color}
        fill-opacity="1"
        class="cursor-pointer"
        onclick={() => onPickEvent(bar.ev)}
        onkeydown={(e) => onEventKey(e, bar.ev)}
        role="button"
        tabindex="0"
      >
        <title>
          {bar.ev.track?.class_name ?? bar.ev.kind} — {dt.hms(bar.ev.at)}
        </title>
      </rect>
    {/each}

    <!-- playhead -->
    {#if playheadX !== null}
      <line
        x1={playheadX} y1={0}
        x2={playheadX} y2={height}
        stroke="var(--color-baba-accent)"
        stroke-width="2"
        vector-effect="non-scaling-stroke"
        style="pointer-events: none"
      />
    {/if}
  </svg>
 </div>

  <!-- Detail strip: 10-min window centered on the playhead. Segments slide
       right→left under a fixed centered playhead while video plays. The
       whole strip is clickable for fine-grained seeking; arrow keys nudge
       by ±5s when focused. -->
  {#if playheadMs !== null && stripWindowStart !== null}
    <div class="border-t border-baba-border bg-baba-panel-2 px-2 pt-1 pb-1">
      <div class="mb-0.5 flex items-baseline justify-between text-2xs text-baba-text-faint">
        <span>−5 min</span>
        <span class="text-baba-text-muted">{t("recordings_strip_hint")}</span>
        <span class="font-medium text-baba-text-muted">
          {dt.hms(playheadMs)}
        </span>
        <span>+5 min</span>
      </div>
      <svg
        viewBox="0 0 {STRIP_VB_WIDTH} {STRIP_HEIGHT}"
        preserveAspectRatio="none"
        class="block w-full touch-none select-none rounded outline-none ring-baba-accent/40 hover:ring-2 focus-visible:ring-2"
        class:cursor-grabbing={stripDragging}
        class:cursor-grab={!stripDragging}
        style:height="{STRIP_HEIGHT}px"
        role="slider"
        tabindex="0"
        aria-label={t("recordings_strip_label")}
        aria-valuemin={stripWindowStart}
        aria-valuemax={stripWindowStart + STRIP_WINDOW_MS}
        aria-valuenow={playheadMs}
        onpointerdown={onStripPointerDown}
        onpointermove={onStripPointerMove}
        onpointerup={onStripPointerUp}
        onpointercancel={onStripPointerUp}
        onclick={onStripClick}
        onwheel={onStripWheel}
        onkeydown={onStripKey}
      >
        <!-- minute ticks -->
        {#each stripMinuteTicks as tick (tick.label)}
          <line
            x1={tick.x} y1={0}
            x2={tick.x} y2={STRIP_HEIGHT}
            stroke="var(--color-baba-border)"
            stroke-width="0.5"
            vector-effect="non-scaling-stroke"
          />
          <text
            x={tick.x + 2} y={10}
            class="fill-baba-text-faint"
            style="font: 9px ui-sans-serif, system-ui, sans-serif; pointer-events: none;"
          >{tick.label}</text>
        {/each}

        <!-- segments -->
        {#each stripSegments as seg (seg.rec.id)}
          <rect
            x={seg.x} y={14}
            width={seg.width} height={STRIP_HEIGHT - 16}
            fill={seg.tone === "playing" ? "var(--color-baba-accent)" : "var(--color-baba-text-muted)"}
            fill-opacity={seg.tone === "other" ? 0.3 : 0.5}
            style="pointer-events: none"
          />
        {/each}

        <!-- sighting bars -->
        {#each stripEvents as bar (bar.ev.id)}
          <rect
            x={bar.x} y={STRIP_HEIGHT - 8}
            width={bar.width} height={6}
            rx="1.5"
            fill={bar.color}
            fill-opacity="0.85"
            style="pointer-events: none"
          />
        {/each}

        <!-- centered playhead (stays fixed; the world moves under it) -->
        <line
          x1={STRIP_CENTER_X} y1={0}
          x2={STRIP_CENTER_X} y2={STRIP_HEIGHT}
          stroke="var(--color-baba-accent)"
          stroke-width="2"
          vector-effect="non-scaling-stroke"
          style="pointer-events: none"
        />
      </svg>
    </div>
  {/if}
</div>
