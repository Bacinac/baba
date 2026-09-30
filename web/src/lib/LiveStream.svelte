<script lang="ts">
  // Live H264/H265 video from a go2rtc-managed camera over HTTP fragmented-MP4
  // (go2rtc /api/stream.mp4 → fetch → MediaSource), which passes cleanly through
  // the Cloudflare tunnel where WebRTC media and WS-upgrade both fail. Pure
  // passthrough — no server-side transcoding, no continuous decode pipeline.
  //
  // The parent can pass `onready` to hook into `loadedmetadata` (e.g. to size
  // a detection-overlay canvas) and `bindVideo` to capture the <video> element.

  import { onDestroy } from "svelte";
  import { connectLive, type LiveHandle, type LiveState } from "$lib/live";

  let {
    slug,
    class: cls = "",
    onready,
    bindVideo,
    onstate,
    preferSubstream = false,
    hasSubstream = false,
  }: {
    slug: string;
    class?: string;
    onready?: () => void;
    bindVideo?: (el: HTMLVideoElement) => void;
    /** Notified of transport state: "connecting" on (re)connect, "error" with
     *  a reason when a stream fails (transient reconnect OR a terminal failure
     *  like an unsupported codec / deleted camera). Lets the parent show a
     *  visible "unavailable" state instead of an invisible black <video>. */
    onstate?: (state: LiveState, detail?: string) => void;
    /** Ask go2rtc for the cheap `{slug}_sub` feed instead of mainstream.
     *  Used for grid thumbnails. Honoured only when `hasSubstream` is true —
     *  otherwise go2rtc has no `_sub` registered and the connect would fail. */
    preferSubstream?: boolean;
    /** Parent must indicate whether the camera has a configured substream
     *  (cam.substream_url !== null), since this component doesn't fetch the
     *  Camera itself. */
    hasSubstream?: boolean;
  } = $props();

  let video: HTMLVideoElement | undefined = $state();
  let live: LiveHandle | null = null;
  let streamName = $derived(preferSubstream && hasSubstream ? `${slug}_sub` : slug);

  // Public demo build (VITE_BABA_DEMO): no go2rtc / live backend, so the MSE
  // video can never fill and would sit as a black "no signal" box. Render this
  // camera's hand-reviewed, blurred still instead — a real static asset written
  // by build-demo.sh at /demo-still-<slug>.jpg. The connect effect below no-ops
  // on its own (no <video> is rendered, so `video` stays undefined and it
  // early-returns) — no MSE, no reconnect storm.
  const DEMO = !!import.meta.env.VITE_BABA_DEMO;

  // Reconnect whenever the target stream changes (e.g. navigating between
  // cameras reuses this component). Reading `streamName` + `video` inside the
  // effect makes it re-run on either change; the old connection is closed in
  // the cleanup. Previously connect ran once in onMount, so a slug change kept
  // the first camera's stream forever.
  $effect(() => {
    const name = streamName;
    const el = video;
    if (!el) return;
    bindVideo?.(el);
    live = connectLive(el, name, onstate);
    return () => {
      if (live) { live.close(); live = null; }
    };
  });

  onDestroy(() => {
    if (live) { live.close(); live = null; }
  });
</script>

{#if DEMO}
  <img
    src="/demo-still-{slug}.jpg"
    alt=""
    draggable="false"
    class={cls}
    onload={() => onready?.()}
  />
{:else}
  <video
    bind:this={video}
    autoplay
    muted
    playsinline
    class={cls}
    onloadedmetadata={() => onready?.()}
  ></video>
{/if}
