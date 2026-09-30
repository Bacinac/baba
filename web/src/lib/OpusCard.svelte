<script lang="ts">
  // OPUS · Library connection panel. Lives on the face-recognition settings
  // page because that is what the import feeds: library photographs are
  // enrolled as FACE references only, so this is a face-recognition input,
  // not an AI provider.
  //
  // No separate Test button: the server pings the library with the token
  // before storing it and rejects a bad one, so saving is testing. The token
  // is never sent back, so the field cannot be prefilled — an existing
  // connection shows as `configured` instead.
  //
  // Save is gated on a real EDIT, not on the fields being non-empty: the
  // browser autofills a saved password into a password input regardless of
  // `autocomplete="off"`; `new-password` stops that, and `tokenEdited` only
  // flips on a keystroke, so an autofill that races the mount cannot pass.

  import { api, type OpusStatus } from "$lib/api";
  import { formatNumber, Card, Tag, SaveButton } from "$lib/kit";
  import { t } from "$lib/i18n";
  import { onMount } from "svelte";

  let status = $state<OpusStatus | null>(null);
  let baseUrl = $state("");
  let token = $state("");
  let savedUrl = $state("");
  let tokenEdited = $state(false);
  let loading = $state(true);
  let saving = $state(false);
  let saved = $state(false);
  let error = $state<string | null>(null);

  async function refresh() {
    try {
      status = await api.opusStatus();
      if (status.base_url) {
        baseUrl = status.base_url;
        savedUrl = status.base_url;
      }
      token = "";
      tokenEdited = false;
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
      status = await api.opusSetSettings({
        base_url: baseUrl.trim(),
        token: tokenEdited && token.trim() ? token.trim() : null,
      });
      savedUrl = baseUrl.trim();
      token = "";
      tokenEdited = false;
      saved = true;
      setTimeout(() => (saved = false), 2500);
    } catch (e) {
      error = (e as Error).message;
    } finally {
      saving = false;
    }
  }

  let dirty = $derived(
    status?.configured
      ? baseUrl.trim() !== savedUrl || (tokenEdited && !!token.trim())
      : !!baseUrl.trim() && tokenEdited && !!token.trim(),
  );
</script>

<Card title={t("opus_title")}>
  {#snippet actions()}
    {#if loading}
      <Tag tone="quiet">…</Tag>
    {:else if status?.configured && status.reachable}
      <Tag tone="ok">
        {t("opus_connected")}{status.people !== null
          ? ` · ${t("opus_people_count").replace("{n}", formatNumber(status.people, { maximumFractionDigits: 1 }))}`
          : ""}
      </Tag>
    {:else if status?.configured}
      <Tag tone="err">
        {t("opus_unreachable")}
      </Tag>
    {:else}
      <Tag tone="quiet">
        {t("opus_not_configured")}
      </Tag>
    {/if}
  {/snippet}

  <p class="text-s text-baba-text-faint">{t("opus_help")}</p>

  <label class="block">
    <span class="block text-s text-baba-text-muted">{t("opus_base_url")}</span>
    <input
      id="opus-base-url"
      type="url"
      bind:value={baseUrl}
      placeholder="http://192.0.2.103:8095"
      class="w-full rounded border border-baba-border bg-baba-panel-2 px-2 py-1.5 font-mono text-m"
    />
  </label>

  <label class="block">
    <span class="block text-s text-baba-text-muted">{t("opus_token")}</span>
    <input
      id="opus-token"
      type="password"
      autocomplete="new-password"
      bind:value={token}
      oninput={() => (tokenEdited = true)}
      placeholder={status?.configured ? t("opus_token_keep") : ""}
      class="w-full rounded border border-baba-border bg-baba-panel-2 px-2 py-1.5 font-mono text-m"
    />
    <span class="mt-1 block text-s text-baba-text-faint">{t("opus_token_hint")}</span>
  </label>

  <div class="flex flex-wrap items-center gap-2">
    <SaveButton {dirty} {saving} label={t("opus_save")} onclick={save} />
  </div>

  {#if saved}
    <p class="text-m text-emerald-400">{t("opus_saved")}</p>
  {/if}

  {#if error}
    <p class="text-m text-red-400">{error}</p>
  {/if}

  {#if status?.configured && !status.reachable && status.error}
    <p class="text-m text-red-400">{status.error}</p>
  {/if}
</Card>
