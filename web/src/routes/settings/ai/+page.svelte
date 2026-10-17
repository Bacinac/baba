<script lang="ts">
  import { onMount } from "svelte";
  import { AiProviderCard, type AiRole } from "$lib/kit";
  import { api, type AiSettings } from "$lib/api";
  import { t } from "$lib/i18n";

  const roles: AiRole[] = [{ key: "image", label: t("ai_model") }];

  let ai = $state<AiSettings | null>(null);
  let error = $state<string | null>(null);

  onMount(async () => {
    try {
      ai = await api.getAiSettings();
    } catch (e) {
      error = (e as Error).message;
    }
  });
</script>

{#if error}
  <p class="text-red-400">{error}</p>
{:else if !ai}
  <p class="text-baba-text-faint">{t("cameras_loading")}</p>
{:else}
  <div class="grid max-w-5xl grid-cols-1 gap-5 lg:grid-cols-2">
    {#each ai.providers as provider (provider)}
      <AiProviderCard {provider} {roles} stored={ai.settings.find((s) => s.provider === provider) ?? null} call={api.ai} />
    {/each}
  </div>
  <p class="mt-4 max-w-2xl text-s text-baba-text-faint">
    {t("ai_priority_hint")}
  </p>
{/if}
