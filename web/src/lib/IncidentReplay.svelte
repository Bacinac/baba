<script lang="ts">
  // Side-by-side evidence for an AI suggestion, over the footage the incident
  // was opened on. One clip, one toggle: the frames never change, only which
  // set of thresholds decides what is drawn on them. That is the whole point —
  // a two-video layout invites you to compare two different moments, which is
  // exactly the mistake this is meant to prevent.
  //
  // Boxes come from the detector's live pre-gate trace, run through the live
  // gate on the server. Nothing here re-derives a threshold decision in the
  // browser; the numbers on screen are the ones production would produce.
  import { api, cameraClipUrl, type IncidentReplay } from "$lib/api";
  import { formatNumber, Button, Picks } from "$lib/kit";
  import { t } from "$lib/i18n";
  let { incidentId, onClose }: { incidentId: string; onClose: () => void } = $props();

  let replay = $state<IncidentReplay | null>(null);
  let error = $state<string | null>(null);
  let showProposed = $state(false);

  let videoEl = $state<HTMLVideoElement | null>(null);
  let canvasEl = $state<HTMLCanvasElement | null>(null);
  let rafId: number | null = null;

  const clipSrc = $derived(
    replay ? cameraClipUrl(replay.camera_id, replay.start_s, replay.end_s) : null,
  );

  $effect(() => {
    let cancelled = false;
    api
      .replayIncident(incidentId)
      .then((r) => {
        if (!cancelled) {
          replay = r;
          error = null;
        }
      })
      .catch((e) => {
        if (!cancelled) error = e instanceof Error ? e.message : String(e);
      });
    return () => {
      cancelled = true;
    };
  });

  // Nearest traced frame to the playhead. The trace is sampled at the camera's
  // own rate, so "nearest" lands within half a frame — close enough that a box
  // sits on the object that produced it.
  function boxesAt(tSeconds: number) {
    if (!replay || !replay.frames.length) return [];
    const targetNs = (replay.start_s + tSeconds) * 1e9;
    let best = replay.frames[0];
    let bestGap = Math.abs(best.t - targetNs);
    for (const f of replay.frames) {
      const gap = Math.abs(f.t - targetNs);
      if (gap < bestGap) {
        best = f;
        bestGap = gap;
      }
    }
    return showProposed ? best.new : best.cur;
  }

  function draw() {
    const v = videoEl;
    const c = canvasEl;
    if (!v || !c) return;
    const w = v.clientWidth;
    const h = v.clientHeight;
    if (c.width !== w || c.height !== h) {
      c.width = w;
      c.height = h;
    }
    const ctx = c.getContext("2d");
    if (!ctx) return;
    ctx.clearRect(0, 0, w, h);
    // Amber = what is live today, emerald = what the suggestion would give.
    // The colour carries the mode so a screenshot is still unambiguous.
    const stroke = showProposed ? "#34d399" : "#f59e0b";
    ctx.lineWidth = 2;
    ctx.font = "12px ui-monospace, monospace";
    for (const b of boxesAt(v.currentTime)) {
      const x = b.x * w;
      const y = b.y * h;
      const bw = b.w * w;
      const bh = b.h * h;
      ctx.strokeStyle = stroke;
      // Solid = birth-eligible (can start a NEW track). Dashed = published but
      // maintain-only: it keeps an existing track alive and nothing more.
      ctx.setLineDash(b.b ? [] : [5, 4]);
      ctx.strokeRect(x, y, bw, bh);
      ctx.setLineDash([]);
      const label = `${b.c} ${formatNumber(b.p, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
      const tw = ctx.measureText(label).width;
      ctx.fillStyle = "rgba(0,0,0,0.65)";
      ctx.fillRect(x, Math.max(0, y - 15), tw + 6, 15);
      ctx.fillStyle = stroke;
      ctx.fillText(label, x + 3, Math.max(11, y - 4));
    }
  }

  function tick() {
    draw();
    rafId = requestAnimationFrame(tick);
  }

  $effect(() => {
    if (!videoEl) return;
    rafId = requestAnimationFrame(tick);
    return () => {
      if (rafId !== null) cancelAnimationFrame(rafId);
      rafId = null;
    };
  });

  // Redraw immediately on toggle, so the swap is visible while paused too.
  $effect(() => {
    showProposed;
    draw();
  });

  function delta(n: number): string {
    return n > 0 ? `+${formatNumber(n, { maximumFractionDigits: 1 })}` : formatNumber(n, { maximumFractionDigits: 1 });
  }
</script>

<div class="mt-3 rounded border border-baba-border bg-baba-panel-2 p-3">
  <div class="mb-2 flex items-center gap-2">
    <Picks
      picks={[{ key: "current", label: t("incident_compare_current") }, { key: "proposed", label: t("incident_compare_proposed") }]}
      chosen={[showProposed ? "proposed" : "current"]}
      onpick={(k) => (showProposed = k === "proposed")}
    />
    <div class="ml-auto"><Button size="small" onclick={onClose}>
      {t("incident_compare_close")}
    </Button></div>
  </div>

  {#if error}
    <p class="text-s text-rose-400">{error}</p>
  {:else if !replay}
    <p class="text-s text-baba-text-faint">{t("incident_comparing")}</p>
  {:else}
    <div class="relative overflow-hidden rounded bg-black">
      <!-- svelte-ignore a11y_media_has_caption -->
      <video
        bind:this={videoEl}
        src={clipSrc}
        controls
        autoplay
        muted
        preload="metadata"
        class="block max-h-[60vh] w-full"
      ></video>
      <canvas
        bind:this={canvasEl}
        class="pointer-events-none absolute left-0 top-0 h-full w-full"
      ></canvas>
    </div>

    <p class="mt-2 text-s text-baba-text-faint">{t("incident_compare_hint")}</p>

    {#if replay.summary.length}
      <div class="mt-2 overflow-x-auto">
        <table class="w-full text-s">
          <thead class="text-baba-text-faint">
            <tr>
              <th class="py-1 text-left font-normal">{t("incident_compare_class")}</th>
              <th class="py-1 text-right font-normal">{t("incident_compare_births")}</th>
              <th class="py-1 text-right font-normal">{t("incident_compare_published")}</th>
            </tr>
          </thead>
          <tbody class="tabular-nums">
            {#each replay.summary as s (s.class_name)}
              {@const bd = s.proposed_births - s.current_births}
              <tr class="border-t border-baba-border/50">
                <td class="py-1 font-mono">{s.class_name}</td>
                <td class="py-1 text-right">
                  <span class="text-amber-400">{formatNumber(s.current_births, { maximumFractionDigits: 1 })}</span>
                  <span class="text-baba-text-faint">→</span>
                  <span class="text-emerald-400">{formatNumber(s.proposed_births, { maximumFractionDigits: 1 })}</span>
                  {#if bd !== 0}
                    <span class={bd > 0 ? "text-emerald-400" : "text-rose-400"}>({delta(bd)})</span>
                  {/if}
                </td>
                <td class="py-1 text-right text-baba-text-muted">
                  {formatNumber(s.current_published, { maximumFractionDigits: 1 })} → {formatNumber(s.proposed_published, { maximumFractionDigits: 1 })}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}

    {#if replay.not_shown.length}
      <p class="mt-2 text-s text-amber-400/80">
        {t("incident_compare_not_shown")}
        <span class="font-mono">{replay.not_shown.join(", ")}</span>
      </p>
    {/if}
  {/if}
</div>
