<script lang="ts">
  import { Card, Tag, SaveButton } from "$lib/kit";
  // Immich connection panel. Lives on the face-recognition settings page
  // because that's what the import feeds: Immich photos are enrolled as FACE
  // references only (the body vector of a head-and-shoulders crop is
  // meaningless), so this is a face-recognition input, not an AI provider.
  //
  // Unlike AiProviderCard there's no separate Test button: the server pings
  // Immich with the key before storing it and rejects a bad one, so saving is
  // testing. The key is never sent back to us, so the field can't be
  // prefilled — an existing connection shows as `configured` instead.
  //
  // That last point is why Save is gated on a real EDIT, not on the fields
  // being non-empty. `apiKey` starts empty and nothing in this component ever
  // fills it, yet the button was live on a freshly loaded page with the key
  // box showing dots: the browser had autofilled it with a saved password.
  // `autocomplete="off"` does not stop Chrome doing that to a password input;
  // `new-password` does. Left alone, one click would have replaced a working
  // Immich key with a login password (the server's ping rejects it, so it
  // fails loudly rather than breaking the connection — but the operator is
  // still being offered an action that can only go wrong).

  import { api, type ImmichStatus } from "$lib/api";
  import { t } from "$lib/i18n";
  import { onMount } from "svelte";

  let status = $state<ImmichStatus | null>(null);
  let baseUrl = $state("");
  let apiKey = $state("");
  // The saved address, to compare against. Not the key — we never see it.
  let savedUrl = $state("");
  // Set only by a real keystroke in the key field. An autofill that races the
  // mount cannot pass through here.
  let keyEdited = $state(false);
  let loading = $state(true);
  let saving = $state(false);
  let saved = $state(false);
  let error = $state<string | null>(null);

  async function refresh() {
    try {
      status = await api.immichStatus();
      if (status.base_url) {
        baseUrl = status.base_url;
        savedUrl = status.base_url;
      }
      // Discard anything the browser dropped into the key box before we
      // finished loading.
      apiKey = "";
      keyEdited = false;
    } catch (e) {
      error = (e as Error).message;
    } finally {
      loading = false;
    }
  }

  onMount(refresh);

  async function save() {
    saving = true;
    saved = false;
    error = null;
    try {
      status = await api.immichSetSettings({
        base_url: baseUrl.trim(),
        // Omitted when untouched: the server keeps the stored key, so the
        // address can be corrected without hunting the key down again.
        api_key: keyEdited && apiKey.trim() ? apiKey.trim() : null,
      });
      savedUrl = baseUrl.trim();
      apiKey = "";
      keyEdited = false;
      saved = true;
      setTimeout(() => (saved = false), 2500);
    } catch (e) {
      error = (e as Error).message;
    } finally {
      saving = false;
    }
  }

  // A first connection needs both. An existing one needs an actual change.
  let dirty = $derived(
    status?.configured
      ? baseUrl.trim() !== savedUrl || (keyEdited && !!apiKey.trim())
      : !!baseUrl.trim() && keyEdited && !!apiKey.trim(),
  );
</script>

<Card title={t("immich_title")}>
  {#snippet actions()}
    {#if loading}
      <Tag tone="quiet">…</Tag>
    {:else if status?.configured && status.reachable}
      <Tag tone="ok">
        {t("immich_connected")}{status.version ? ` · v${status.version}` : ""}
      </Tag>
    {:else if status?.configured}
      <Tag tone="err">
        {t("immich_unreachable")}
      </Tag>
    {:else}
      <Tag tone="quiet">
        {t("immich_not_configured")}
      </Tag>
    {/if}
  {/snippet}

  <p class="text-s text-baba-text-faint">{t("immich_help")}</p>

  <label class="block">
    <span class="block text-s text-baba-text-muted">{t("immich_base_url")}</span>
    <input
      type="url"
      bind:value={baseUrl}
      placeholder="http://192.0.2.102:2283"
      class="w-full rounded border border-baba-border bg-baba-panel-2 px-2 py-1.5 font-mono text-m"
    />
  </label>

  <label class="block">
    <span class="block text-s text-baba-text-muted">{t("immich_api_key")}</span>
    <input
      type="password"
      autocomplete="new-password"
      bind:value={apiKey}
      oninput={() => (keyEdited = true)}
      placeholder={status?.configured ? t("immich_api_key_keep") : ""}
      class="w-full rounded border border-baba-border bg-baba-panel-2 px-2 py-1.5 font-mono text-m"
    />
    <span class="mt-1 block text-s text-baba-text-faint">{t("immich_api_key_hint")}</span>
  </label>

  <div class="flex flex-wrap items-center gap-2">
    <SaveButton {dirty} {saving} label={t("immich_save")} onclick={save} />
  </div>

  {#if saved}
    <p class="text-m text-emerald-400">{t("immich_saved")}</p>
  {/if}

  {#if error}
    <p class="text-m text-red-400">{error}</p>
  {/if}

  {#if status?.configured && !status.reachable && status.error}
    <p class="text-m text-red-400">{status.error}</p>
  {/if}
</Card>
