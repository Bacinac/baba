<script lang="ts">
  // Help index — the concept guide. Cards link into individual articles.
  // Reference material lives with the settings it documents (inline hints);
  // this section is the "how it works" model.
  import { i18n } from "$lib/kit";
  import { t, type MessageKey } from "$lib/i18n";
  import { HELP_ARTICLES } from "$lib/help/content";

  const groups = ["concepts", "operating"] as const;
</script>

<div class="max-w-4xl space-y-8">
  {#each groups as g (g)}
    {@const items = HELP_ARTICLES.filter((a) => a.group === g)}
    {#if items.length > 0}
      <section>
        <h3 class="mb-3 text-s uppercase tracking-wide text-baba-text-faint">
          {t(`help_group_${g}` as MessageKey)}
        </h3>
        <div class="space-y-2">
          {#each items as a (a.slug)}
            <a
              href="/help/{a.slug}"
              class="block rounded-lg border border-baba-border bg-baba-panel p-4 transition-colors hover:border-baba-accent/60 hover:bg-baba-panel-2"
            >
              <div class="text-m font-semibold">{a.title[i18n.locale]}</div>
              <div class="mt-1 text-s text-baba-text-faint">{a.summary[i18n.locale]}</div>
            </a>
          {/each}
        </div>
      </section>
    {/if}
  {/each}
</div>
