<script lang="ts">
  import { theme, type Theme } from "$lib/kit";
  import { t } from "$lib/i18n";
  import { auth } from "$lib/auth.svelte";
  import { api } from "$lib/api";

  // light → dark → system → light → …
  const NEXT: Record<Theme, Theme> = {
    light: "dark",
    dark: "system",
    system: "light",
  };

  function cycle() {
    const next = NEXT[theme.theme];
    theme.setTheme(next);
    if (auth.user) {
      // Fire-and-forget: header switch stays responsive; if the PATCH fails
      // the local change is already applied and persisted to localStorage.
      api.patchPreferences({ theme: next }).catch(() => {});
    }
  }

  function currentLabel(): string {
    if (theme.theme === "light") return t("theme_light");
    if (theme.theme === "dark") return t("theme_dark");
    return t("theme_system");
  }
</script>

<button
  type="button"
  onclick={cycle}
  aria-label={t("theme_label")}
  title="{t('theme_label')}: {currentLabel()}"
  class="grid h-8 w-8 place-items-center rounded text-baba-text-muted hover:bg-baba-panel-2 hover:text-baba-text"
>
  {#if theme.theme === "light"}
    <!-- sun -->
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41" />
    </svg>
  {:else if theme.theme === "dark"}
    <!-- moon -->
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
    </svg>
  {:else}
    <!-- system (monitor) -->
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <rect x="2" y="4" width="20" height="13" rx="2" ry="2" />
      <line x1="8" y1="21" x2="16" y2="21" />
      <line x1="12" y1="17" x2="12" y2="21" />
    </svg>
  {/if}
</button>
