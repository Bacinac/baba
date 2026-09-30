<script lang="ts">
  import { i18n, type Locale } from "$lib/kit";
  import { t } from "$lib/i18n";
  import { auth } from "$lib/auth.svelte";
  import { api } from "$lib/api";

  const NEXT: Record<Locale, Locale> = { hr: "en", en: "hr" };

  function cycle() {
    const next = NEXT[i18n.locale];
    i18n.set(next);
    if (auth.user) {
      api.patchPreferences({ locale: next }).catch(() => {});
    }
  }
</script>

<button
  type="button"
  onclick={cycle}
  aria-label={t("language_label")}
  title="{t('language_label')} ({i18n.locale.toUpperCase()})"
  class="grid h-8 w-8 place-items-center rounded text-baba-text-muted hover:bg-baba-panel-2 hover:text-baba-text"
>
  <span class="text-xs font-semibold tracking-wider">{i18n.locale.toUpperCase()}</span>
</button>
