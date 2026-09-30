<script lang="ts">
  import { page } from "$app/state";
  import { i18n } from "$lib/kit";
  import { t } from "$lib/i18n";
  import { articleBySlug } from "$lib/help/content";
  import { renderMarkdown } from "$lib/help/markdown";

  let slug = $derived(page.params.slug ?? "");
  let article = $derived(articleBySlug(slug));
  // Content is authored in-repo (help/content.ts), rendered by the constrained
  // markdown renderer that escapes before adding its own tags — {@html} is safe
  // here because the input is trusted and the output can't inject.
  let html = $derived(article ? renderMarkdown(article.body[i18n.locale]) : "");
</script>

<div class="max-w-3xl">
  <a href="/help" class="text-s text-baba-text-faint hover:text-baba-accent">← {t("help_title")}</a>

  {#if article}
    <h1 class="mt-2 mb-6 text-2xl font-semibold">{article.title[i18n.locale]}</h1>
    <!-- eslint-disable-next-line svelte/no-at-html-tags -->
    <div class="text-m text-baba-text">{@html html}</div>
  {:else}
    <p class="mt-6 text-m text-baba-text-faint">{t("help_not_found")}</p>
  {/if}
</div>
