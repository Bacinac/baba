// Live transport for the camera grid + detail view.
//
// Transport = HTTP fragmented-MP4 (fetch streaming) → MediaSource, see mse.ts.
// This is the path that works through our Cloudflare tunnel; the browser's
// WebSocket upgrade fails there, and WebRTC media (a direct peer connection to
// a LAN ICE candidate) can't traverse an HTTP-only tunnel at all. Kept as a
// thin seam so a WebRTC sub-second fast-path for LAN access can be layered on
// later (try WebRTC, fall back to this) without touching callers.

import { connectMSE } from "$lib/mse";

export type LiveState = "connecting" | "playing" | "error";

export interface LiveHandle {
  close(): void;
}

export function connectLive(
  video: HTMLVideoElement,
  slug: string,
  onState?: (state: LiveState, detail?: string) => void,
): LiveHandle {
  onState?.("connecting");
  // Report recovery, not only failure. `scheduleReconnect` calls the error
  // callback for every TRANSIENT drop as well, and nothing ever said the
  // stream had come back — so the "Stream nedostupan" overlay latched on the
  // first routine reconnect and stayed over a video that was playing fine.
  const onPlaying = () => onState?.("playing");
  video.addEventListener("playing", onPlaying);
  const handle = connectMSE(video, slug, (detail) => onState?.("error", detail));
  return {
    close() {
      video.removeEventListener("playing", onPlaying);
      try { handle.close(); } catch { /* ignore */ }
    },
  };
}

