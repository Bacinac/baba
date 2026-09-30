<script lang="ts">
  import { byLabel } from "$lib/order";
  import { onMount, onDestroy } from "svelte";
  import { api, cameraClipUrl, thumbnailUrl, eventsFeed,
    type BabaEvent, type Camera, type Recording, type Sighting,
    type SuppressedTrack,
    type Feed,
  } from "$lib/api";
  import { page } from "$app/state";
  import { t, type MessageKey } from "$lib/i18n";
  import { Hint, Picks, Toggle, Tag, formatNumber } from "$lib/kit";
  import { formatDuration } from "$lib/format";
  import { dt } from "$lib/datetime.svelte";
  import { playback } from "$lib/playback.svelte";
  import Timeline from "$lib/Timeline.svelte";

  // Deep link from Home's recent-sighting click (/activity?cam&t). Parsed at
  // INIT via page (SSR-safe, same pattern as settings/cameras ?tab) so the
  // FIRST load already targets the right camera + day and there's no race with
  // a default load. `dlSeekMs` is the moment to auto-cut a clip at once its
  // recordings are in (consumed in load()).
  const dlCam = page.url.searchParams.get("cam") ?? "";
  const dlSeekMs = (() => {
    const t = page.url.searchParams.get("t");
    if (!t) return null;
    const ms = new Date(t).getTime();
    return isFinite(ms) ? ms : null;
  })();

  // Activity = one surface for "what happened" + "watch the footage". A
  // scrubable recording timeline (real segments + event markers, works in
  // both activity- and continuous-recording modes — in 24/7 you can scrub
  // through quiet stretches too) sits above the sightings feed. Both follow
  // the same time window chosen by the range presets. This absorbed the old
  // standalone Recordings page.

  const SIGHTING_LIMIT = 300;
  let sightings = $state<Sighting[]>([]);
  // The feed is capped; when it comes back full there is older activity in
  // the drawn window that this list does not contain, and the operator has
  // to be told rather than left to infer it from an empty stretch.
  const truncated = $derived(sightings.length >= SIGHTING_LIMIT);
  let recordings = $state<Recording[]>([]);
  // What the zone and static filters kept out of the feed. Loaded only when
  // asked for: the answer to "did the camera see anything there" used to be
  // reachable only by pulling the segment and watching it.
  let suppressed = $state<SuppressedTrack[]>([]);
  let showSuppressed = $state(false);
  let cameras = $state<Camera[]>([]);
  let loading = $state(true);
  let error = $state<string | null>(null);

  let cameraFilter = $state<string>(dlCam);
  let classGroup = $state<"" | "person" | "vehicle" | "pet">("");
  let rangePreset = $state<"hour" | "today" | "yesterday" | "7d" | "all">(playback.activityRange);
  // That was a one-shot copy of a store the user's preferences fill in later:
  // this page can mount before `playback.setFromPrefs` runs, so the operator's
  // chosen default range was silently replaced by the built-in one. Adopt it
  // when it arrives — but only until the operator picks a range here, after
  // which their click owns the value.
  let presetPicked = $state(false);
  $effect(() => {
    const fromPrefs = playback.activityRange;
    if (!presetPicked) rangePreset = fromPrefs;
  });
  // A specific calendar day (YYYY-MM-DD) to browse old footage. "" = use the
  // range preset above. When set, it overrides the preset; the timeline shows
  // that whole day and you pick the time by scrubbing.
  let customDate = $state<string>(dlSeekMs !== null ? ymd(new Date(dlSeekMs)) : "");

  function ymd(d: Date): string {
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  }
  // Computed fresh on read, not frozen at mount — otherwise a page left open
  // across midnight caps the date picker at yesterday.
  const todayYmd = (): string => ymd(new Date());
  function shiftDay(delta: number): void {
    const base = customDate ? new Date(customDate + "T00:00:00") : new Date();
    base.setDate(base.getDate() + delta);
    if (base.getTime() <= Date.now()) customDate = ymd(base);
  }
  function pickPreset(p: typeof rangePreset): void {
    presetPicked = true;
    rangePreset = p;
    customDate = "";
  }

  // The window [start, end] the timeline + feed span, in ms. Snapped at each
  // load so "now"-relative presets don't drift mid-render.
  let windowStartMs = $state(Date.now() - 86_400_000);
  let windowEndMs = $state(Date.now());

  const CLASS_GROUPS: Record<string, string[]> = {
    person: ["person"],
    vehicle: ["car", "truck", "bus", "motorcycle"],
    pet: ["dog", "cat", "bird"],
  };
  // Deduped by id: the keyed each below treats a duplicate key as a hard
  // render error, and one bad row from the server must not freeze the page.
  let shown = $derived.by(() => {
    const base = classGroup
      ? sightings.filter((s) => (CLASS_GROUPS[classGroup] ?? []).includes(s.class_name))
      : sightings;
    const seen = new Set<string>();
    return base.filter((s) => !seen.has(s.id) && (seen.add(s.id), true));
  });

  // Timeline markers are the SIGHTINGS (person/car/… visits), not raw zone
  // enter/exit/dwell events — those aren't what you want to scan for and
  // often have no clip to play. Adapt each sighting into the BabaEvent shape
  // the Timeline expects; clicking a marker plays it like a feed row. Follows
  // the class filter since it derives from `shown`.
  const CLASS_IDS: Record<string, number> = {
    person: 0, bicycle: 1, car: 2, motorcycle: 3, bus: 5, train: 6, truck: 7,
    bird: 14, cat: 15, dog: 16,
  };
  let timelineEvents = $derived<BabaEvent[]>(
    shown.map((s) => ({
      id: s.id,
      kind: "sighting",
      at: s.started_at,
      payload: {},
      camera: s.camera,
      track: {
        id: s.track_ids[0] ?? "",
        class_id: CLASS_IDS[s.class_name] ?? 0,
        class_name: s.identity?.name ?? s.class_name,
        duration_s: s.duration_s,
        thumbnail_path: s.thumbnail_path,
      },
      recording: s.recording,
    })),
  );
  let visibleCameras = $derived(
    cameraFilter ? cameras.filter((c) => c.id === cameraFilter) : cameras,
  );

  // Map a range preset to an absolute [start, end] ms window. `all` returns
  // null → caller derives the window from the fetched data.
  function presetWindow(p: typeof rangePreset): { start: number; end: number } | null {
    const now = Date.now();
    if (p === "hour") return { start: now - 3_600_000, end: now };
    if (p === "7d") return { start: now - 7 * 86_400_000, end: now };
    if (p === "today") {
      const t = new Date(); t.setHours(0, 0, 0, 0);
      return { start: t.getTime(), end: now };
    }
    if (p === "yesterday") {
      const t = new Date(); t.setHours(0, 0, 0, 0);
      return { start: t.getTime() - 86_400_000, end: t.getTime() };
    }
    return null; // all
  }

  // A specific day → that whole calendar day (clamped to now). Else the preset.
  function dayWindow(d: string): { start: number; end: number } {
    const start = new Date(d + "T00:00:00");
    const end = new Date(start); end.setDate(end.getDate() + 1);
    return { start: start.getTime(), end: Math.min(end.getTime(), Date.now()) };
  }

  // Race guard — this load fetches sightings + up to 2000 recordings, so it's
  // slow enough that a fast filter toggle can land responses out of order.
  let loadSeq = 0;
  // Deep-link target (from Home's recent-sighting click, /activity?cam&t): the
  // absolute ms to auto-cut a clip at once the recordings for it have loaded.
  let pendingSeekMs: number | null = dlSeekMs;

  // Cleared when nothing is in flight, not when the newest call returns: a
  // superseded load used to abandon the spinner it raised and leave clearing it
  // to a successor that might never answer.
  let inflight = 0;

  async function load(silent = false) {
    const myseq = ++loadSeq;
    if (!silent) loading = true;
    error = null;
    inflight++;
    try {
      const w = customDate ? dayWindow(customDate) : presetWindow(rangePreset);
      const since = w ? new Date(w.start).toISOString() : undefined;
      const until = w ? new Date(w.end).toISOString() : undefined;
      const cam = cameraFilter || undefined;
      const [sg, recs, sup] = await Promise.all([
        // Fixed cap regardless of range. Over a week or "Sve" the server
        // returns the newest 300 and nothing says so, while the timeline below
        // draws the whole window — so an operator scrubbing to Tuesday saw an
        // empty list and read it as "nothing happened".
        api.listSightings({ camera_id: cam, since, until, limit: SIGHTING_LIMIT }),
        // MERGED coverage runs, not raw 60 s segments: a continuous camera-day
        // is one row, so the all-cameras view shows the WHOLE day's footage
        // (the old capped segment fetch covered only the most recent hours and
        // read as "recording keeps stopping"). Playback never needs the raw
        // segment row — clips are cut by camera + absolute time.
        api.recordingsCoverage({ camera_id: cam, since, until }),
        showSuppressed
          ? api.listSuppressedTracks({ camera_id: cam, since, until })
          : Promise.resolve([] as SuppressedTrack[]),
      ]);
      if (myseq !== loadSeq) return;
      sightings = sg;
      recordings = recs;
      suppressed = sup;
      if (pendingSeekMs !== null) {
        // Data's in — auto-play what the deep-link pointed at. The link carries
        // the event's finalize time (≈ the sighting's END), so prefer the
        // sighting that CONTAINS it and play from its start (server-computed
        // seek), not that trailing instant; fall back to a raw cut if none
        // matches (e.g. footage gap). One-shot.
        const target = pendingSeekMs;
        pendingSeekMs = null;
        const s = sg.find((x) => {
          const st = new Date(x.started_at).getTime();
          const en = new Date(x.ended_at).getTime();
          return target >= st - 2000 && target <= en + 2000;
        });
        if (s) playSighting(s);
        else scrubTo(target);
      }
      if (w) {
        windowStartMs = w.start;
        windowEndMs = w.end;
      } else {
        // "all": span from the earliest datum to now.
        const starts = [
          ...sg.map((s) => new Date(s.started_at).getTime()),
          ...recs.map((r) => new Date(r.started_at).getTime()),
        ];
        windowStartMs = starts.length ? Math.min(...starts) : Date.now() - 7 * 86_400_000;
        windowEndMs = Date.now();
      }
    } catch (e) {
      if (myseq === loadSeq) error = (e as Error).message;
    } finally {
      inflight--;
      if (inflight === 0) loading = false;
    }
  }

  $effect(() => {
    void cameraFilter;
    void rangePreset;
    void customDate;
    load();
  });

  // Live refresh. The feed was one-shot: new activity only showed on a manual
  // filter/range change. Now we listen to the same events SSE the Events page
  // uses (fires on every track_finalized — i.e. exactly when a sighting can
  // appear) and refetch, but ONLY when the window ends at "now" — a historical
  // day or "yesterday" must not jump around under the operator. Debounced so a
  // burst of finalizations coalesces into one refetch; silent so no spinner
  // flash, and it never disturbs an open clip (the player state is separate).
  let feed: Feed | null = null;
  let liveTimer: ReturnType<typeof setTimeout> | null = null;
  function isLiveWindow(): boolean {
    return !customDate && rangePreset !== "yesterday";
  }
  function scheduleLiveRefresh(): void {
    if (!isLiveWindow()) return;
    if (liveTimer) clearTimeout(liveTimer);
    liveTimer = setTimeout(() => { liveTimer = null; load(true); }, 1500);
  }

  onMount(async () => {
    try { cameras = await api.listCameras(); } catch (e) { console.error(e); }
    // The events SSE fires on EVERY event insert (zone enter/exit, parked, …).
    // A sighting only appears/extends on `track_finalized`, so gate on that —
    // otherwise every zone crossing would trigger a full refetch. Respect the
    // camera filter too (the NOTIFY payload carries camera_id).
    feed = eventsFeed({
      message: (p) => {
        if (p.kind !== "track_finalized") return;
        if (cameraFilter && p.camera_id !== cameraFilter) return;
        scheduleLiveRefresh();
      },
      resync: scheduleLiveRefresh,
    });
  });

  onDestroy(() => {
    feed?.close();
    if (liveTimer) clearTimeout(liveTimer);
  });

  // --- formatting helpers ---
  // Parked duration as H:MM:SS (a clock), not "4h 19m 12s" — read at a glance.
  // The OCR returns a plate stripped to letters and digits; a Croatian plate is
  // read and written ZG-9648-GZ. Anything that does not fit that shape is shown
  // exactly as it was read rather than forced into it.
  function formatPlate(plate: string | null | undefined): string {
    if (!plate) return "";
    const m = /^([A-Z]{2})(\d{3,5})([A-Z]{1,2})$/.exec(plate);
    return m ? `${m[1]}-${m[2]}-${m[3]}` : plate;
  }

  function fmtClock(seconds: number): string {
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const sec = Math.floor(seconds % 60);
    return `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
  }

  // --- inline player. Plays a small server-cut faststart CLIP window, not the
  // raw 300s segment. Seeking into a large fMP4 segment (no seek index) makes
  // the browser pull everything from the segment start to the target — over
  // Cloudflare that never finishes ("spins"). The clip endpoint returns a small
  // MP4 that STARTS near the target and transcodes HEVC→H.264, so playback is
  // instant. `active` stays the recording under the playhead for camera +
  // timeline context; the <video> src is the clip window. ---
  let playerEl: HTMLDivElement | null = $state(null);
  let videoEl: HTMLVideoElement | null = $state(null);
  let active = $state<Recording | null>(null);

  // Back to the plain feed: unmount the player (video stops with it).
  function closePlayer() {
    active = null;
    clipStartMs = null;
    clipEndMs = null;
    clipPlayEndMs = null;
  }
  let clipStartMs = $state<number | null>(null);
  // The window we ASKED the cutter for — this pair, and only this pair, builds
  // the clip URL.
  let clipEndMs = $state<number | null>(null);
  // The window we were actually GIVEN (measured off the loaded video). Kept
  // apart from the requested one on purpose: a cut is never frame-exact — it
  // snaps back to the keyframe at or before the start and carries the last
  // frame's display time — so the delivered clip always runs a frame or two
  // LONGER than asked. Feeding that measurement back into the URL asks for a
  // slightly longer window every time the video loads, which loads again, which
  // asks for longer... an endless reload loop that never plays a frame. It bit
  // west (HEVC, 32 s visit): every round paid a fresh GPU transcode and the
  // player sat dead at 0:00. Patio survived only by accident — a 6 m visit hits
  // the server's 120 s clamp, so round two asks for 120 s, gets 120 s, and the
  // loop lands on a fixed point.
  let clipPlayEndMs = $state<number | null>(null);
  // Was this clip opened as a VISIT (bounded) or a timeline SCRUB (continuous)?
  let clipIsVisit = $state(false);
  let pendingSeekSec = $state<number | null>(null);
  let playheadMs = $state<number | null>(null);

  // Clip window. Playing a VISIT frames the visit itself: a beat before it
  // starts (the walk-in) through a beat after it ends (the walk-out), instead
  // of a fixed block from the trigger — which gave a 3 s visit 57 s of empty
  // scene and cut a 22-minute one off after the first minute. The server
  // clamps anything long (120 s lossless copy, 45 s if it must re-encode) and
  // onLoadedMetadata re-syncs to what actually arrived; the rest of a long
  // visit is a timeline scrub away. Scrubbing the timeline has no end to aim
  // at, so it falls back to the fixed window.
  // Clip padding is the OPERATOR's setting (Settings → Preferences), the same
  // operator playback preferences — this page used to hardcode its own.
  // Note what "before the event" buys: a visit starts when the TRACK is born,
  // already several seconds into the real entrance (the birth gate wants
  // consecutive confident frames while the camera ramps up from idle fps), so
  // a small pre-roll still opens mid-approach. That is a knob to turn, not a
  // fudge to hide in code.
  const leadMs = $derived(playback.clipPrerollS * 1000);
  const tailMs = $derived(playback.clipPostrollS * 1000);
  const CLIP_MIN_MS = 8000;    // floor so a 1 s blip still yields a real clip
  const CLIP_LEN_MS = 60_000;  // fallback window when no end is known

  const clipSrc = $derived(
    active && clipStartMs !== null && clipEndMs !== null && clipEndMs > clipStartMs
      ? cameraClipUrl(active.camera.id, clipStartMs / 1000, clipEndMs / 1000)
      : null,
  );

  // Aim the clip window at an absolute time on `rec`'s camera; seek to it.
  // `absUntilMs` is the visit's end when one is known — the window then spans
  // the whole visit plus padding rather than a fixed block.
  function setClip(rec: Recording, absTargetMs: number, absUntilMs?: number): void {
    active = rec;
    const isVisit = absUntilMs !== undefined;
    // Remembered for onEnded: a visit stops at its end, a scrub rolls on.
    clipIsVisit = isVisit;
    // A timeline scrub aims at an exact instant — no pre-roll, or the frame
    // under the cursor would not be the frame that plays.
    const startMs = absTargetMs - (isVisit ? leadMs : 0);
    const wantEnd = isVisit ? absUntilMs + tailMs : startMs + CLIP_LEN_MS;
    clipStartMs = startMs;
    // Clamp to the live edge FIRST, floor SECOND. The other order let a scrub
    // at the live edge collapse the window to nothing, and a zero-length window
    // is not something the cutter can answer — it came back a 500. Asking a few
    // seconds past the live edge is harmless: the cut simply ends where the
    // footage does.
    clipEndMs = Math.max(Math.min(wantEnd, Date.now()), startMs + CLIP_MIN_MS);
    clipPlayEndMs = null;
    // A VISIT plays from the top so the walk-in is on screen; seeking to the
    // trigger is what dropped the operator in mid-scene. A timeline SCRUB has
    // no entrance to show — land exactly where they pointed.
    pendingSeekSec = isVisit ? 0 : Math.max(0, (absTargetMs - startMs) / 1000);
  }

  function pickRecording(r: Recording, seekSec: number, absUntilMs?: number): void {
    setClip(r, new Date(r.started_at).getTime() + seekSec * 1000, absUntilMs);
  }

  function scrubTo(absMs: number): void {
    // Inside the loaded clip → just seek (cheap; it's a small file). Measured
    // against what was DELIVERED where we know it: the server clamps long
    // windows (120 s lossless copy, 45 s re-encoded), so a 20-minute visit
    // comes back short and seeking past its real end would land in nothing.
    const loadedEndMs = clipPlayEndMs ?? clipEndMs;
    if (videoEl && clipStartMs !== null && loadedEndMs !== null
        && absMs >= clipStartMs && absMs < loadedEndMs) {
      const t = (absMs - clipStartMs) / 1000;
      videoEl.currentTime = isFinite(videoEl.duration)
        ? Math.min(t, Math.max(0, videoEl.duration - 0.05))
        : Math.max(0, t);
      playheadMs = absMs;
      return;
    }
    // Outside → re-cut a clip around the new time (same camera if we can).
    let pick: Recording | null = null;
    for (const r of recordings) {
      const s = new Date(r.started_at).getTime();
      const e = r.ended_at ? new Date(r.ended_at).getTime() : Date.now();
      if (absMs < s || absMs >= e) continue;
      if (active && r.camera.id === active.camera.id) { pick = r; break; }
      if (!pick) pick = r;
    }
    if (pick) setClip(pick, absMs);
  }

  function pickEvent(ev: BabaEvent): void {
    // Purely absolute-time resolution: `recordings` are merged coverage runs
    // with synthetic ids, so an event's raw segment id can never match one —
    // and its `seek_seconds` is segment-relative, meaningless against a run.
    const t = new Date(ev.at).getTime();
    for (const r of recordings) {
      if (r.camera.id !== ev.camera.id) continue;
      const s = new Date(r.started_at).getTime();
      const e = r.ended_at ? new Date(r.ended_at).getTime() : Date.now();
      if (t >= s && t < e) { setClip(r, t); return; }
    }
  }

  // Play the footage covering a sighting from the feed. The player is sticky,
  // so it stays in view — no scroll needed. Coverage rows are MERGED runs with
  // synthetic ids, so resolve by camera + ABSOLUTE time, not by segment id.
  //
  // `recording.start_at` IS the visit start in absolute time; `seek_seconds` is
  // an offset INSIDE the raw 60 s segment that covers it — two different
  // reference frames. Adding them (as this did) lands the clip a segment-offset
  // PAST the visit: a 39 s visit at 04:41:50 opened at 04:42:39, after it had
  // already ended, so the operator saw an empty scene. Use the absolute time
  // alone; setClip's CLIP_LEAD_MS already backs up a few seconds so the
  // walk-in is in frame.
  // Play one clip window (a segment) by finding the recording that covers its
  // start. A visit has one segment (person / short) or two bookends (long
  // vehicle: arrival, departure) — same cut mechanism either way.
  function playWindow(cameraId: string, startIso: string, endIso: string): void {
    const absMs = new Date(startIso).getTime();
    const untilMs = new Date(endIso).getTime();
    for (const r of recordings) {
      if (r.camera.id !== cameraId) continue;
      const rs = new Date(r.started_at).getTime();
      const re = r.ended_at ? new Date(r.ended_at).getTime() : Date.now();
      if (absMs >= rs && absMs < re) {
        pickRecording(r, (absMs - rs) / 1000, untilMs);
        return;
      }
    }
  }

  // Row click plays the first segment — the whole clip for a person, the
  // arrival bookend for a split vehicle. The departure has its own button.
  // Each row's started_at/ended_at IS its clip window (the backend set an
  // arrival/departure row to its own bookend), so a row plays exactly the
  // moment it represents.
  function playSighting(s: Sighting): void {
    if (!s.recording) return;
    playWindow(s.camera.id, s.started_at, s.ended_at);
  }

  // rAF playhead (ontimeupdate is too coarse for a smooth cursor).
  let rafId: number | null = null;
  function updatePlayhead(): void {
    if (!videoEl || clipStartMs === null) return;
    playheadMs = clipStartMs + videoEl.currentTime * 1000;
  }
  function tickPlayhead(): void {
    if (!videoEl || videoEl.paused || videoEl.ended) { rafId = null; return; }
    updatePlayhead();
    rafId = requestAnimationFrame(tickPlayhead);
  }
  function startTicking(): void { if (rafId === null) rafId = requestAnimationFrame(tickPlayhead); }
  function stopTicking(): void { if (rafId !== null) { cancelAnimationFrame(rafId); rafId = null; } }

  function onLoadedMetadata(): void {
    if (!videoEl) return;
    if (pendingSeekSec !== null && isFinite(videoEl.duration)) {
      videoEl.currentTime = Math.min(pendingSeekSec, Math.max(0, videoEl.duration - 0.1));
      pendingSeekSec = null;
    }
    // Record what we were actually given — NOT into clipEndMs, which builds the
    // URL (see clipPlayEndMs). scrubTo and onEnded read this one.
    if (clipStartMs !== null && isFinite(videoEl.duration) && videoEl.duration > 0) {
      clipPlayEndMs = clipStartMs + videoEl.duration * 1000;
    }
    updatePlayhead();
    if (playback.autoplay) {
      videoEl.play().catch(() => { /* autoplay can be blocked */ });
    }
  }
  function onPlay(): void { startTicking(); }
  function onPause(): void { stopTicking(); updatePlayhead(); }
  function onSeeked(): void { updatePlayhead(); }

  // Clip window change recreates the <video> (#key) → cancel in-flight rAF.
  $effect(() => { clipStartMs; return stopTicking; });

  // At clip end, roll into the next window on the same camera — but ONLY when
  // the operator is scrubbing the timeline, where "keep going" is the whole
  // point of a continuous recorder.
  //
  // A VISIT is a bounded thing they asked to see. Rolling on served footage of
  // an empty patio after it, and because a window change recreates the <video>
  // (#key clipStartMs) every roll reset the progress bar to zero — one visit
  // reading as several clips, each running 0→100. The bar should measure the
  // duration the card promises, so the visit is now cut as ONE file (the
  // server caps allow 20 min) and simply ends when it ends.
  function onEnded(): void {
    if (clipIsVisit) { playheadMs = null; return; }
    const nextStart = clipPlayEndMs ?? clipEndMs;
    if (nextStart === null || !active) return;
    if (nextStart >= Date.now() - CLIP_MIN_MS) { playheadMs = null; return; }
    clipStartMs = nextStart;
    clipEndMs = Math.max(Math.min(nextStart + CLIP_LEN_MS, Date.now()), nextStart + CLIP_MIN_MS);
    clipPlayEndMs = null;
    pendingSeekSec = 0;
  }
</script>

<!-- Filters -->
<div class="mb-4 flex flex-wrap items-center gap-2">
  <select
    bind:value={cameraFilter}
    class="rounded border border-baba-border bg-baba-panel-2 px-3 py-2 text-m"
  >
    <option value="">{t("events_filter_all_cameras")}</option>
    {#each byLabel(cameras, (c) => c.name) as c (c.id)}
      <option value={c.id}>{c.name}</option>
    {/each}
  </select>
  <Picks
    picks={[["", "identities_filter_all"], ["person", "identities_filter_person"], ["vehicle", "identities_filter_vehicle"], ["pet", "identities_filter_pet"]].map(([key, word]) => ({ key, label: t(word as never) }))}
    chosen={[classGroup]}
    onpick={(k) => (classGroup = k as typeof classGroup)}
  />
  <Picks
    picks={(["hour", "today", "yesterday", "7d", "all"] as const).map((key) => ({ key, label: t(`events_range_${key}`) }))}
    chosen={customDate ? [] : [rangePreset]}
    onpick={(k) => pickPreset(k as typeof rangePreset)}
  />

  <!-- Pick a specific day to browse older footage; scrub the timeline for the
       exact time. Overrides the range presets when set. -->
  <div class="flex items-center rounded border bg-baba-panel-2 {customDate ? 'border-baba-accent' : 'border-baba-border'}">
    <button
      type="button"
      onclick={() => shiftDay(-1)}
      class="px-1.5 py-1 text-s text-baba-text-muted hover:bg-baba-panel"
      aria-label={t("activity_prev_day")}
    >◀</button>
    <input
      type="date"
      max={todayYmd()}
      bind:value={customDate}
      class="w-[7.5rem] border-0 bg-transparent p-0 text-s text-baba-text [color-scheme:dark] focus:outline-none"
    />
    <button
      type="button"
      onclick={() => shiftDay(1)}
      disabled={!customDate || customDate >= todayYmd()}
      class="px-1.5 py-1 text-s text-baba-text-muted hover:bg-baba-panel disabled:opacity-30"
      aria-label={t("activity_next_day")}
    >▶</button>
  </div>

  <label class="flex items-center gap-2 text-s text-baba-text-muted">
    <Toggle size="small" checked={showSuppressed} onclick={() => { showSuppressed = !showSuppressed; load(); }} />
    {t("sightings_show_suppressed")}
  </label>

  <span class="ml-auto text-s text-baba-text-faint">
    {formatNumber(shown.length)} {t("sightings_count_suffix")}
  </span>
  <Hint text={t("hint_activity_visits")} article="activity-and-visits" />
</div>

<!-- Scrubable recording timeline for the selected window -->
{#if visibleCameras.length > 0}
  <Timeline
    {windowStartMs}
    {windowEndMs}
    cameras={visibleCameras}
    {recordings}
    events={timelineEvents}
    activeCameraId={active?.camera.id ?? null}
    {playheadMs}
    onPickRecording={pickRecording}
    onPickEvent={pickEvent}
    onSeekTo={scrubTo}
  />
{/if}

<svelte:window onkeydown={(e) => { if (e.key === "Escape" && active) closePlayer(); }} />

<!-- Inline player — only takes space once something is playing. Sticky so it
     stays in view while scrolling a long feed (no jump-to-top on play).
     Closes via the ✕ or Escape — back to the plain feed. -->
{#if active && clipSrc}
  <div
    bind:this={playerEl}
    class="sticky top-2 z-20 mt-4 overflow-hidden rounded-lg border border-baba-border bg-black shadow-lg"
  >
    <button
      type="button"
      onclick={closePlayer}
      title={t("activity_player_close")}
      aria-label={t("activity_player_close")}
      class="absolute right-2 top-2 z-10 grid h-8 w-8 place-items-center rounded-full bg-black/70 text-xl leading-none text-white hover:bg-black/90"
    >×</button>
    {#key clipStartMs}
      <!-- svelte-ignore a11y_media_has_caption -->
      <video
        bind:this={videoEl}
        src={clipSrc}
        controls
        autoplay={playback.autoplay}
        playsinline
        preload="metadata"
        onloadedmetadata={onLoadedMetadata}
        onplay={onPlay}
        onpause={onPause}
        onseeked={onSeeked}
        onended={onEnded}
        class="block max-h-[60vh] w-full bg-black"
      ></video>
    {/key}
  </div>
{/if}

<!-- Feed -->
<div class="mt-6">
{#if loading}
  <p class="text-baba-text-faint">{t("cameras_loading")}</p>
{:else if error}
  <p class="text-red-400">{error}</p>
{:else if shown.length === 0}
  <p class="rounded border border-baba-border bg-baba-panel p-4 text-m text-baba-text-faint">
    {t("sightings_empty")}
  </p>
{:else}
  {#if truncated}
    <p
      class="mb-2 rounded border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-s text-amber-200"
    >
      {t("sightings_truncated")}
    </p>
  {/if}
  <ul class="space-y-2">
    {#each shown as s (s.id)}
      <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
      <li
        class="flex items-stretch gap-3 rounded-lg border border-baba-border bg-baba-panel p-3 transition-colors {s.recording ? 'cursor-pointer hover:border-baba-accent/60 hover:bg-baba-panel-2' : 'opacity-70'}"
        role={s.recording ? 'button' : undefined}
        tabindex={s.recording ? 0 : -1}
        aria-disabled={s.recording ? undefined : 'true'}
        onclick={() => playSighting(s)}
        onkeydown={(e) => {
          if (s.recording && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); playSighting(s); }
        }}
      >
        <div class="relative h-20 w-32 shrink-0 overflow-hidden rounded bg-black">
          {#if s.thumbnail_path}
            <!-- Scene thumbnail with the detection box already baked in (see
                 event-manager SnapshotBaker) — replaces the old play button; the
                 whole row is clickable and plays anyway. Shows WHAT was detected
                 at a glance, e.g. a "dog" that's actually the robot mower. -->
            <img src={thumbnailUrl(s.thumbnail_path)} alt="" class="h-full w-full object-cover" loading="lazy" />
          {:else if s.crop_path}
            <img src={thumbnailUrl(s.crop_path)} alt="" class="h-full w-full object-cover" loading="lazy" />
          {:else}
            <div class="grid h-full w-full place-items-center text-s text-baba-text-faint">—</div>
          {/if}
          {#if s.plate_text}
            <!-- A plate is not appearance, so it is not the amber "guessed from
                 body" badge and not the face one either. It shows what was read,
                 because that is checkable by eye against the car. -->
            <span class="absolute right-1 top-1 font-mono [--tag-font:var(--fs-2xs)]"><Tag tone="busy" onpicture>{formatPlate(s.plate_text)}</Tag></span>
          {:else if s.face_verified}
            <span class="absolute right-1 top-1 [--tag-font:var(--fs-2xs)]"><Tag tone="ok" onpicture>✓ {t("identities_sighting_face")}</Tag></span>
          {:else if s.identity}
            <!-- Recognized, but by BODY only (face-anchored body chain) — a
                 face never confirmed this sighting, so it's a strong guess, not
                 a certainty. Yellow, distinct from the green face badge. -->
            <span class="absolute right-1 top-1 [--tag-font:var(--fs-2xs)]"><Tag tone="warn" onpicture>✓ {t("identities_sighting_no_face")}</Tag></span>
          {/if}
        </div>

        <div class="flex min-w-0 flex-1 flex-col justify-between">
          <div class="flex items-baseline gap-2">
            <span
              class="text-m font-semibold"
              class:text-emerald-400={s.identity && s.face_verified}
              class:text-amber-300={s.identity && !s.face_verified}
            >
              {#if s.identity}{s.identity.name}{:else}<span class="text-baba-text-muted">{s.class_name}</span>{/if}
            </span>
            <span class="text-s text-baba-text-faint">{t("sightings_on")} {s.camera.name}</span>
            <span class="ml-auto text-s text-baba-text-faint">
              <!-- Same date · start – end (dur) notation for every row; an
                   arrival/departure just carries a small kind badge before it. -->
              {#if s.kind === "left_with_vehicle"}<span class="text-baba-accent">🚗 {t("activity_left_with_vehicle")}{#if s.vehicle_name} — {s.vehicle_name}{/if}</span> · {/if}
              <!-- The person who drove off in it, folded onto the car's own row
                   rather than repeated as a second blank one beside it. -->
              {#if s.left_with}<span class="text-baba-accent">🚗 {t("activity_left_with_vehicle")} — {s.left_with}</span> · {/if}
              {#if s.kind === "arrival"}<span class="text-baba-accent">⬇️ {t("activity_arrived")}</span> · {/if}
              {#if s.kind === "departure"}<span class="text-baba-accent">🅿 {t("activity_parked_for")} {fmtClock(s.visit_duration_s)}</span>
                <span class="text-baba-accent">⬆️ {t("activity_departed")}</span> · {/if}
              {dt.day(s.started_at)} · {dt.hms(s.started_at)} – {dt.hms(s.ended_at)}
              <span class="text-baba-text-muted">({formatDuration(s.duration_s)})</span>
            </span>
          </div>

          <div class="mt-1 flex flex-wrap items-center gap-1">
            {#if s.zones.length === 0}
              <span class="text-s text-baba-text-faint">{t("sightings_no_zones")}</span>
            {:else}
              {#each s.zones as z (z.zone_id)}
                <Tag tone="quiet">
                  {z.zone_name ?? "?"}
                  {#if z.dwell_s > 0}
                    <span class="text-baba-text-faint">· {formatDuration(z.dwell_s)}</span>
                  {/if}
                </Tag>
              {/each}
            {/if}
            <span class="ml-auto text-2xs text-baba-text-faint">
              {s.track_count} {t("sightings_tracks")} · {s.observations} {t("sightings_observations")}
              {#if !s.recording}· <span class="text-baba-text-muted">{t("events_no_recording")}</span>{/if}
            </span>
          </div>
        </div>
      </li>
    {/each}
  </ul>
{/if}
</div>

<!-- What the filters kept out of the feed. A zone dwell or the static
     threshold decides what is worth showing; neither decides what the system
     remembers, so this is always answerable without opening the footage. -->
{#if showSuppressed}
  <div class="mt-6">
    <h3 class="mb-2 text-m font-semibold text-baba-text-muted">
      {t("sightings_suppressed_title")}
    </h3>
    {#if suppressed.length === 0}
      <p class="rounded border border-baba-border bg-baba-panel p-3 text-s text-baba-text-faint">
        {t("sightings_suppressed_empty")}
      </p>
    {:else}
      <ul class="space-y-1">
        {#each suppressed as track (track.id)}
          <li class="flex flex-wrap items-baseline gap-2 rounded border border-baba-border bg-baba-panel px-3 py-2 text-s">
            <span class="font-semibold text-baba-text-muted">{track.class_name}</span>
            <span class="text-baba-text-faint">{t("sightings_on")} {track.camera.name}</span>
            <Tag tone="quiet">
              {t(`sightings_suppressed_${track.suppressed_reason.replace("-", "_")}` as MessageKey)}
            </Tag>
            <span class="ml-auto text-baba-text-faint">
              {dt.day(track.started_at)} · {dt.hms(track.started_at)} – {dt.hms(track.ended_at)}
              <span class="text-baba-text-muted">({track.observations} {t("sightings_observations")})</span>
            </span>
          </li>
        {/each}
      </ul>
    {/if}
  </div>
{/if}
