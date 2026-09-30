<script lang="ts">
  import { Tag } from "$lib/kit";
  // One grid tile: a still frame the browser re-asks for every second.
  //
  // This is deliberately NOT a stream. An <img> fed Motion-JPEG keeps whatever
  // it last received forever once the connection ends — silently, with no
  // error and no retry — so a dead tile is indistinguishable from a quiet
  // camera. That is how one tile sat eight minutes stale among six live ones,
  // and how every api restart froze the whole grid until someone reloaded.
  //
  // A tile that re-asks cannot fail that way: a request that dies is simply
  // replaced by the next one a second later. And because each request either
  // succeeds or fails, the tile knows how old its picture is and can say so —
  // which the stream could never do.
  //
  // Each frame is preloaded off-DOM and swapped in only once decoded, so the
  // visible image never blanks between refreshes.
  import { t } from "$lib/i18n";
  import { formatUptime } from "$lib/format";
  import { dt } from "$lib/datetime.svelte";
  import type { ParkedVehicle } from "$lib/api/types";

  let {
    cameraId,
    name,
    boxes,
    parked = [],
  }: { cameraId: string; name: string; boxes: boolean; parked?: ParkedVehicle[] } = $props();

  const PERIOD_MS = 1000;
  // The ring is decimated to the adaptive detection rate (1 fps when idle), so
  // a couple of missed seconds is normal jitter, not a fault. Past this the
  // picture is genuinely not current and the tile says so rather than
  // presenting an old frame as live.
  const STALE_AFTER_MS = 8000;
  const NEVER_AFTER_MISSES = 10;

  let src = $state<string | null>(null);
  let lastOkMs = $state(0);
  let misses = $state(0);
  let nowMs = $state(Date.now());

  const stale = $derived(lastOkMs > 0 && nowMs - lastOkMs > STALE_AFTER_MS);
  const neverLoaded = $derived(lastOkMs === 0 && misses < NEVER_AFTER_MISSES);
  // Long enough that a slow first frame is not accused, short enough that
  // the operator is not left reading "connecting" at a camera that is not
  // going to answer.
  const noFrames = $derived(lastOkMs === 0 && misses >= NEVER_AFTER_MISSES);

  $effect(() => {
    // Re-arm whenever the camera or the overlay toggle changes.
    const id = cameraId;
    const withBoxes = boxes;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let pending: HTMLImageElement | null = null;

    const fetchOne = () => {
      if (cancelled) return;
      const img = new Image();
      pending = img;
      const url = `/api/cameras/${id}/live.jpg?boxes=${withBoxes ? 1 : 0}&t=${Date.now()}`;
      img.onload = () => {
        if (cancelled) return;
        src = url;
        lastOkMs = Date.now();
        schedule();
      };
      // A failed second is not an event — the next one is already on its way.
      // Only the accumulated silence matters, and `stale` reports that.
      img.onerror = () => {
        if (cancelled) return;
        // A failed second is not an event — but a camera that has NEVER
        // produced a frame answers 503 every second forever, and until this
        // counted them the tile said "Spajanje…" indefinitely, identical to
        // one that is genuinely still connecting.
        misses += 1;
        schedule();
      };
      img.src = url;
    };

    const schedule = () => {
      if (cancelled) return;
      timer = setTimeout(fetchOne, PERIOD_MS);
    };

    fetchOne();
    const ticker = setInterval(() => (nowMs = Date.now()), 1000);

    return () => {
      cancelled = true;
      if (timer !== null) clearTimeout(timer);
      clearInterval(ticker);
      if (pending) {
        pending.onload = null;
        pending.onerror = null;
      }
    };
  });
</script>

<div class="relative mb-3 aspect-video overflow-hidden rounded bg-black">
  {#if src}
    <img {src} alt={name} class="h-full w-full object-cover" class:opacity-40={stale} />
  {/if}
  {#if parked.length}
    <!-- On the picture, where the operator is looking — not in a caption row
         below it. Top-left, because the bottom edge belongs to the staleness
         banner and the boxes overlay draws over the scene itself. -->
    <div class="absolute left-1 top-1 flex flex-col items-start gap-1">
      <!-- How long it has stood there, not when it arrived. A clock time with
           no date reads as today's: a car parked yesterday at 16:46 showed
           "16:46" at 11:33 the next morning, which looks like the future. The
           duration is also the thing being asked — "since when" is one
           subtraction away from useless while "for how long" is the answer. -->
      {#each parked as p (p.place)}
        <Tag onpicture title={dt.full(p.since)}>
          🅿 {p.name || t("live_parked_unknown")} · {p.place} · {formatUptime(
            Math.max(0, (nowMs - new Date(p.since).getTime()) / 1000),
          )}
        </Tag>
      {/each}
    </div>
  {/if}
  {#if neverLoaded}
    <div
      class="absolute inset-0 grid place-items-center text-s text-baba-text-faint"
    >
      {t("live_tile_connecting")}
    </div>
  {:else if noFrames}
    <div
      class="absolute inset-0 grid place-items-center px-2 text-center text-s text-rose-200"
    >
      {t("live_tile_no_frames")}
    </div>
  {:else if stale}
    <!-- Say it outright. A dimmed old frame with no label is exactly the lie
         the stream used to tell. -->
    <div
      class="absolute inset-x-0 bottom-0 bg-rose-900/80 px-2 py-1 text-center text-s text-rose-100"
    >
      {t("live_tile_stale").replace("{s}", String(Math.round((nowMs - lastOkMs) / 1000)))}
    </div>
  {/if}
</div>
