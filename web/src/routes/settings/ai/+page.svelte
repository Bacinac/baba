<script lang="ts">
  import { onMount } from "svelte";
  import { api, type AiSettings } from "$lib/api";
  import { t } from "$lib/i18n";
  import AiProviderCard from "$lib/AiProviderCard.svelte";

  let stored = $state<AiSettings[]>([]);
  let loading = $state(true);
  let error = $state<string | null>(null);

  async function refresh() {
    loading = true;
    error = null;
    try {
      stored = await api.listAiSettings();
    } catch (e) {
      error = (e as Error).message;
    } finally {
      loading = false;
    }
  }

  function findStored(provider: string): AiSettings | null {
    return stored.find((s) => s.provider === provider) ?? null;
  }

  onMount(refresh);
</script>

{#if loading}
  <p class="text-baba-text-faint">{t("cameras_loading")}</p>
{:else if error}
  <p class="text-red-400">{error}</p>
{:else}
  <div class="grid max-w-5xl grid-cols-1 gap-5 lg:grid-cols-2">
    {#key stored}
      <AiProviderCard
        provider="anthropic"
        label={t("ai_provider_anthropic")}
        apiKeyPlaceholder="sk-ant-…"
        apiKeyHelpUrl="https://console.anthropic.com/settings/keys"
        initialStored={findStored("anthropic")}
      />
      <AiProviderCard
        provider="openai"
        label={t("ai_provider_openai")}
        apiKeyPlaceholder="sk-…"
        apiKeyHelpUrl="https://platform.openai.com/api-keys"
        initialStored={findStored("openai")}
      />
    {/key}
  </div>
  <p class="mt-4 max-w-2xl text-s text-baba-text-faint">
    {t("ai_priority_hint")}
  </p>
{/if}
