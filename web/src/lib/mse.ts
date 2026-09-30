// Live player for go2rtc over HTTP fragmented-MP4 (fetch streaming) → MSE.
//
// Why not WebSocket MSE: go2rtc's lower-latency transport is a WS (/api/ws),
// but the browser's WS upgrade fails through our Cloudflare tunnel (works for a
// header-less server client, fails for a real browser request — a vite-dev/CF
// upgrade quirk). go2rtc's `/api/stream.mp4?src=<slug>` delivers the SAME
// fragmented MP4 over a plain long-lived HTTP GET, which traverses Cloudflare
// like any other HTTP(S) response. We read the response body stream and feed
// the bytes to a MediaSource SourceBuffer. Latency ~1-2s — fine for an NVR.
//
// (A WebRTC fast-path for sub-second LAN viewing can be layered back on top via
// connectLive; this is the transport that reliably works through the tunnel.)

export interface MSEHandle {
  close(): void;
}

const RECONNECT_MAX_MS = 30000;
// Jump to the live edge if playback drifts more than this behind the buffer
// end — keeps latency from accumulating after a stall / tab throttle.
const MAX_DRIFT_S = 2.0;
const LIVE_EDGE_S = 0.5;
// Fallback codec string if the response omits codecs in Content-Type.
const DEFAULT_MIME = 'video/mp4; codecs="avc1.640029,mp4a.40.2"';

export function connectMSE(
  video: HTMLVideoElement,
  slug: string,
  onError?: (detail: string) => void,
): MSEHandle {
  // iOS 17+ dropped MediaSource for ManagedMediaSource; prefer the standard.
  const MS: typeof MediaSource | undefined =
    (window as unknown as { MediaSource?: typeof MediaSource }).MediaSource ||
    (window as unknown as { ManagedMediaSource?: typeof MediaSource }).ManagedMediaSource;

  let closed = false;
  let abort: AbortController | null = null;
  let objectUrl: string | null = null;
  let backoffMs = 1000;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  function teardown() {
    if (abort) { try { abort.abort(); } catch { /* ignore */ } abort = null; }
    if (objectUrl) { try { URL.revokeObjectURL(objectUrl); } catch { /* ignore */ } objectUrl = null; }
  }

  // Terminal failure: a non-retryable error (unsupported codec, HTTP 4xx —
  // camera deleted / bad request). Stop for good instead of re-pulling the
  // live MP4 every 30s forever behind a permanently-black <video>, and surface
  // it so the UI can show "stream unavailable" rather than an invisible black.
  function fail(reason: string) {
    if (closed) return;
    closed = true;
    if (reconnectTimer !== null) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    teardown();
    console.error(`mse ${slug}: ${reason} — not retrying`);
    onError?.(reason);
  }

  function scheduleReconnect(reason: string) {
    if (closed || reconnectTimer !== null) return;
    teardown();
    onError?.(reason);
    const wait = backoffMs;
    backoffMs = Math.min(backoffMs * 2, RECONNECT_MAX_MS);
    console.warn(`mse ${slug}: ${reason}, reconnecting in ${wait}ms`);
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      if (!closed) start();
    }, wait);
  }

  async function start() {
    if (closed) return;
    if (!MS) { console.error(`mse ${slug}: MediaSource unsupported in this browser`); return; }

    const ms = new MS();
    let sb: SourceBuffer | null = null;
    let msOpen = false;
    let pendingMime: string | null = null;
    let seekedToLive = false;
    const queue: Uint8Array[] = [];

    function flush() {
      if (!sb || sb.updating || queue.length === 0) return;
      const chunk = queue[0];
      try {
        sb.appendBuffer(chunk as BufferSource);
        queue.shift();
      } catch (e) {
        if ((e as DOMException)?.name === "QuotaExceededError") {
          trim(); // updateend → flush retries this chunk
        } else {
          scheduleReconnect("appendBuffer failed");
          return;
        }
      }
      jumpLive();
    }

    function trim() {
      if (!sb || sb.updating || sb.buffered.length === 0) return;
      const start0 = sb.buffered.start(0);
      const end = sb.buffered.end(sb.buffered.length - 1);
      if (end - start0 > 10) {
        try { sb.remove(start0, end - 5); } catch { /* ignore */ }
      }
    }

    function jumpLive() {
      if (!sb || sb.buffered.length === 0) return;
      // Seeking before the element has metadata is silently dropped by some
      // browsers — that was the "first plays through the past" bug: the
      // one-shot live jump fired on the very first append (readyState 0),
      // did nothing, and playback started at the buffer start. Wait until
      // the element is actually seekable; flush() retries on every append.
      if (video.readyState < 1) return;
      const end = sb.buffered.end(sb.buffered.length - 1);
      if (!seekedToLive) {
        // go2rtc begins the stream a keyframe back (long-GOP HEVC cameras =
        // many seconds of backlog). Jump straight to the live edge once
        // instead of replaying it.
        seekedToLive = true;
        video.currentTime = Math.max(end - LIVE_EDGE_S, sb.buffered.start(0));
        return;
      }
      const drift = end - video.currentTime;
      if (drift > MAX_DRIFT_S) {
        // Real stall / tab throttle — hard re-sync.
        video.currentTime = end - LIVE_EDGE_S;
        video.playbackRate = 1.0;
      } else if (drift > 1.2) {
        // Creeping latency — catch up smoothly instead of a visible skip.
        video.playbackRate = 1.1;
      } else if (video.playbackRate !== 1.0) {
        video.playbackRate = 1.0;
      }
    }

    function trySetup() {
      if (sb || !msOpen || pendingMime === null) return;
      // Gate on codec support first: e.g. an HEVC (west) camera on a browser
      // without MSE-HEVC would throw in addSourceBuffer, and the old code
      // reconnected + re-threw forever. This is a permanent condition → fail.
      if (MS && !MS.isTypeSupported(pendingMime)) {
        fail(`codec not supported by this browser: ${pendingMime}`);
        return;
      }
      try {
        sb = ms.addSourceBuffer(pendingMime);
        sb.mode = "segments";
        sb.addEventListener("updateend", flush);
        flush();
        // Autoplay can need a nudge after swapping video.src to the MediaSource.
        video.play().catch(() => { /* muted autoplay; ignore rejection */ });
      } catch {
        scheduleReconnect("addSourceBuffer failed");
      }
    }

    const isManaged =
      "ManagedMediaSource" in window &&
      ms instanceof (window as unknown as { ManagedMediaSource: typeof MediaSource }).ManagedMediaSource;
    if (isManaged) {
      (video as unknown as { disableRemotePlayback: boolean }).disableRemotePlayback = true;
      (video as unknown as { srcObject: unknown }).srcObject = ms;
    } else {
      video.srcObject = null;
      objectUrl = URL.createObjectURL(ms);
      video.src = objectUrl;
    }

    ms.addEventListener(
      "sourceopen",
      () => {
        if (closed) return;
        msOpen = true;
        trySetup();
      },
      { once: true },
    );

    abort = new AbortController();
    const localAbort = abort;
    try {
      const resp = await fetch(`/go2rtc/api/stream.mp4?src=${encodeURIComponent(slug)}`, {
        signal: localAbort.signal,
        cache: "no-store",
      });
      if (!resp.ok || !resp.body) {
        // 4xx (except 429 rate-limit) = permanent: camera deleted, bad slug,
        // auth. Don't hammer — fail terminally. 5xx / 429 = transient → retry.
        if (resp.status >= 400 && resp.status < 500 && resp.status !== 429) {
          fail(`http ${resp.status}`);
        } else {
          scheduleReconnect(`http ${resp.status}`);
        }
        return;
      }
      backoffMs = 1000;
      pendingMime = resp.headers.get("content-type") || DEFAULT_MIME;
      trySetup();

      const reader = resp.body.getReader();
      while (!closed) {
        const { done, value } = await reader.read();
        if (done) { scheduleReconnect("stream ended"); return; }
        if (!value || value.length === 0) continue;
        queue.push(value);
        flush();
      }
    } catch (e) {
      if (closed || (e as Error)?.name === "AbortError") return;
      scheduleReconnect((e as Error)?.message || "fetch error");
    }
  }

  start();

  return {
    close() {
      closed = true;
      if (reconnectTimer !== null) { clearTimeout(reconnectTimer); reconnectTimer = null; }
      teardown();
    },
  };
}
