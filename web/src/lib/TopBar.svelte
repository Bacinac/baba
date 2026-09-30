<script lang="ts">
  // What BABA says in the frame's top bar on every page: a live feed that has
  // been lost, the theme and the language. Help and who is signed in are the
  // frame's own.
  import { t } from "$lib/i18n";
  import { live } from "$lib/api";
  import { Tag } from "$lib/kit";
  import LanguageSwitcher from "$lib/LanguageSwitcher.svelte";
  import ThemeSwitcher from "$lib/ThemeSwitcher.svelte";
</script>

<span role="status" class="min-w-0 truncate">
  {#if live.lost}<Tag tone="warn" title={t("live_feed_lost_hint")}>{t("live_feed_lost")}</Tag>{/if}
</span>
<div class="ml-auto flex items-center gap-1">
  <ThemeSwitcher />
  <LanguageSwitcher />
</div>
