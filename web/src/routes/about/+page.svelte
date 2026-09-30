<script lang="ts">
  import { About } from "$lib/kit";
  import { t } from "$lib/i18n";
  import Brand from "$lib/Brand.svelte";
  import { version } from "$lib/version.svelte";
</script>

<About
  tagline={t("about_tagline")}
  intro={t("about_intro")}
  motto={t("about_acronym_html")}
  what={t("about_what")}
  why={t("about_why")}
  stack={t("about_stack")}
  aside={t("about_disclaimer")}
  build={version.booted}
>
  {#snippet brand()}<Brand />{/snippet}
</About>
