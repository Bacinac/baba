<script lang="ts">
  // What BABA says in the frame's top bar on every page: a live feed that has
  // been lost, and help, the theme and the language. Who is signed in is the
  // frame's own.
  import { page } from "$app/state";
  import { t } from "$lib/i18n";
  import { live } from "$lib/api";
  import { Tag } from "$lib/kit";
  import LanguageSwitcher from "$lib/LanguageSwitcher.svelte";
  import ThemeSwitcher from "$lib/ThemeSwitcher.svelte";

  let onHelp = $derived(page.url.pathname.startsWith("/help"));
</script>

<span role="status" class="min-w-0 truncate">
  {#if live.lost}<Tag tone="warn" title={t("live_feed_lost_hint")}>{t("live_feed_lost")}</Tag>{/if}
</span>
<div class="ml-auto flex items-center gap-1">
  <a
    href="/help"
    aria-label={t("nav_help")}
    title={t("nav_help")}
    class="grid h-8 w-8 place-items-center rounded hover:bg-baba-panel-2 hover:text-baba-text
           {onHelp ? 'text-baba-accent' : 'text-baba-text-muted'}"
  >
    <!-- help (circled question mark) -->
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <circle cx="12" cy="12" r="10" />
      <path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3" />
      <line x1="12" y1="17" x2="12.01" y2="17" />
    </svg>
  </a>
  <ThemeSwitcher />
  <LanguageSwitcher />
</div>
