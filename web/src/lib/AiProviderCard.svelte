<script lang="ts">
  // One card per AI provider on the AI settings page. The page renders one of
  // these for Anthropic and one for OpenAI; each card manages its own form
  // state and API calls (load, save, test, delete) for its provider slot.

  import { api, type AiModelOption, type AiSettings, type AiTestResult } from "$lib/api";
  import { dialog, Button, Card, Tag, SaveButton } from "$lib/kit";
  import { t, type MessageKey } from "$lib/i18n";
  import { dt } from "$lib/datetime.svelte";

  type Props = {
    provider: "anthropic" | "openai";
    label: string;
    apiKeyPlaceholder: string;
    apiKeyHelpUrl: string;
    initialStored: AiSettings | null;
  };

  let {
    provider,
    label,
    apiKeyPlaceholder,
    apiKeyHelpUrl,
    initialStored,
  }: Props = $props();

  // Props seed the editable state once; subsequent edits stay local.
  // untrack() makes that intent explicit and silences the rune-reactivity
  // warning we'd otherwise get from referencing props in $state init.
  import { onMount, untrack } from "svelte";
  let stored = $state<AiSettings | null>(untrack(() => initialStored));
  let model = $state(untrack(() => initialStored?.model ?? ""));
  let apiKey = $state("");
  let enabled = $state(untrack(() => initialStored?.enabled ?? true));

  let saving = $state(false);
  let saved = $state(false);
  const dirty = $derived(
    model !== "" &&
      (!!apiKey.trim() || (stored !== null && (model !== stored.model || enabled !== stored.enabled))),
  );

  let models = $state<AiModelOption[] | null>(null);
  let loadingModels = $state(false);
  let modelsError = $state<string | null>(null);
  const listed = $derived((models ?? []).some((m) => m.id === model));
  const modelOffered = $derived(models === null || model === "" || listed);

  async function loadModels() {
    loadingModels = true;
    modelsError = null;
    try {
      const key = apiKey.trim();
      models = (await api.listAiModels(key ? { provider, api_key: key } : { provider })).models;
    } catch (e) {
      models = null;
      modelsError = (e as Error).message;
    } finally {
      loadingModels = false;
    }
  }

  onMount(() => {
    if (stored) void loadModels();
  });

  let testing = $state(false);
  let testResult = $state<AiTestResult | null>(null);
  let error = $state<string | null>(null);

  async function save() {
    saving = true;
    saved = false;
    error = null;
    try {
      const body = {
        provider,
        model,
        enabled,
        api_key: apiKey ? apiKey : undefined,
      };
      stored = await api.saveAiSettings(body);
      apiKey = "";
      saved = true;
      setTimeout(() => (saved = false), 2500);
    } catch (e) {
      error = (e as Error).message;
    } finally {
      saving = false;
    }
  }

  async function test() {
    testing = true;
    testResult = null;
    try {
      const body: { provider: string; api_key?: string; model?: string } = {
        provider,
        model,
      };
      if (apiKey) body.api_key = apiKey;
      testResult = await api.testAiCredentials(body);
    } catch (e) {
      testResult = {
        ok: false,
        model: null,
        latency_ms: 0,
        error: (e as Error).message,
      };
    } finally {
      testing = false;
    }
  }

  async function deleteKey() {
    const ok = await dialog.confirm({
      title: t("dialog_confirm_title"),
      message: t("ai_confirm_delete"),
      confirmLabel: t("dialog_delete"),
      danger: true,
    });
    if (!ok) return;
    try {
      await api.deleteAiSettings(provider);
      stored = null;
      apiKey = "";
      testResult = null;
      model = "";
      models = null;
      modelsError = null;
      enabled = true;
    } catch (e) {
      error = (e as Error).message;
    }
  }
</script>

<Card title={label}>
  {#snippet actions()}
    {#if stored}
      <Tag tone="ok">
        {stored.enabled ? t("zone_enabled" as MessageKey) : t("zone_disabled" as MessageKey)}
      </Tag>
    {:else}
      <Tag tone="quiet">
        {t("ai_never_used")}
      </Tag>
    {/if}
  {/snippet}

  <label class="block">
    <span class="block text-s text-baba-text-muted">{t("ai_model")}</span>
    <select
      bind:value={model}
      disabled={models === null}
      class="w-full rounded border border-baba-border bg-baba-panel-2 px-2 py-1.5 text-m disabled:opacity-60"
    >
      {#if model === ""}
        <option value="" disabled>{t("ai_model_pick")}</option>
      {/if}
      {#if model !== "" && !listed}
        <option value={model}>{models === null ? model : `${model} · ${t("ai_model_unavailable")}`}</option>
      {/if}
      {#each models ?? [] as m (m.id)}
        <option value={m.id}>{m.label}</option>
      {/each}
    </select>
    {#if loadingModels}
      <span class="mt-1 block text-s text-baba-text-faint">{t("ai_models_loading")}</span>
    {:else if modelsError}
      <span class="mt-1 block text-s text-red-400">{t("ai_models_failed")}: {modelsError}</span>
    {:else if models === null}
      <span class="mt-1 block text-s text-baba-text-faint">{t("ai_models_need_key")}</span>
    {:else if !modelOffered}
      <span class="mt-1 block text-s text-red-400">{t("ai_model_unavailable_hint")}</span>
    {/if}
  </label>

  <label class="block">
    <span class="block text-s text-baba-text-muted">{t("ai_api_key")}</span>
    <input
      type="password"
      autocomplete="off"
      bind:value={apiKey}
      onchange={() => apiKey.trim() && loadModels()}
      placeholder={stored ? stored.api_key_masked : apiKeyPlaceholder}
      class="w-full rounded border border-baba-border bg-baba-panel-2 px-2 py-1.5 font-mono text-m"
    />
    <span class="mt-1 block text-s text-baba-text-faint">
      {t("ai_api_key_hint")}
      {#if stored}
        · {t("ai_api_key_stored")}: <span class="font-mono">{stored.api_key_masked}</span>
      {/if}
    </span>
    <a
      class="mt-1 inline-block text-s text-baba-accent hover:underline"
      href={apiKeyHelpUrl}
      target="_blank"
      rel="noreferrer noopener"
    >{t("ai_get_key_link")}</a>
  </label>

  <label class="flex items-center gap-2 text-m text-baba-text-muted">
    <input type="checkbox" bind:checked={enabled} class="accent-baba-accent" />
    {t("ai_enabled")}
  </label>

  <div class="flex flex-wrap items-center gap-2">
    <SaveButton {dirty} {saving} onclick={save} />

    <Button onclick={test} disabled={testing || model === "" || (!stored && !apiKey)}>{testing ? t("ai_testing") : t("ai_test")}</Button>

    {#if stored}
      <div class="ml-auto"><Button tone="danger" onclick={deleteKey}>{t("ai_delete")}</Button></div>
    {/if}
  </div>

  {#if saved}
    <p class="text-m text-emerald-400">{t("ai_saved")}</p>
  {/if}

  {#if error}
    <p class="text-m text-red-400">{error}</p>
  {/if}

  {#if testResult}
    {#if testResult.ok}
      <p class="text-m text-emerald-400">
        ✓ {t("ai_test_ok")} · {testResult.model} · {testResult.latency_ms} ms
      </p>
    {:else}
      <p class="text-m text-red-400">
        ✗ {t("ai_test_failed")}: {testResult.error}
      </p>
    {/if}
  {/if}

  {#if stored}
    <p class="text-s text-baba-text-faint">
      {t("ai_last_used")}:
      {stored.last_used_at ? dt.full(stored.last_used_at) : t("ai_never_used")}
    </p>
  {/if}
</Card>
