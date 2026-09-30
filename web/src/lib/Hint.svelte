<script lang="ts">
  // Inline "?" help, shown at the point of action. A small popover carrying a
  // one-line explanation and, optionally, a deep-link into the concept guide.
  //
  // The explanation text is passed in by the caller (which sources it from the
  // i18n catalogue — the single home of UI copy), so this component is only the
  // mechanism, never a second copy of the words.
  import { t } from "$lib/i18n";
  let { text, article }: { text: string; article?: string } = $props();
  let open = $state(false);
  let root: HTMLSpanElement | null = $state(null);

  function onDocClick(e: MouseEvent) {
    if (root && !root.contains(e.target as Node)) open = false;
  }
  $effect(() => {
    if (open) {
      document.addEventListener("click", onDocClick);
      return () => document.removeEventListener("click", onDocClick);
    }
  });
</script>

<span class="relative inline-block align-middle" bind:this={root}>
  <button
    type="button"
    class="grid h-4 w-4 place-items-center rounded-full border border-baba-border text-2xs leading-none text-baba-text-faint hover:border-baba-accent/60 hover:text-baba-accent"
    aria-label={t("hint_aria")}
    onclick={(e) => { e.stopPropagation(); open = !open; }}
  >?</button>
  {#if open}
    <span
      class="absolute left-1/2 top-6 z-20 w-64 -translate-x-1/2 rounded-lg border border-baba-border bg-baba-panel-2 p-3 text-left text-s font-normal leading-relaxed text-baba-text shadow-lg"
    >
      {text}
      {#if article}
        <a
          href="/help/{article}"
          class="mt-2 block text-baba-accent hover:underline"
          onclick={(e) => e.stopPropagation()}
        >{t("hint_learn_more")} →</a>
      {/if}
    </span>
  {/if}
</span>
