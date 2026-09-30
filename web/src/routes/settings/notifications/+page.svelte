<script lang="ts">
  import { byLabel } from "$lib/order";
  import { onMount } from "svelte";
  import { dialog, Button, Tag, Toggle } from "$lib/kit";
  import {
    api,
    type Camera,
    type DeliveryRow,
    type NotificationChannel,
    type NotificationKind,
    type NotificationRule,
  } from "$lib/api";
  import { t, type MessageKey } from "$lib/i18n";
  import { dt } from "$lib/datetime.svelte";

  let channels = $state<NotificationChannel[]>([]);
  let rules = $state<NotificationRule[]>([]);
  let cameras = $state<Camera[]>([]);
  let loading = $state(true);
  let error = $state<string | null>(null);

  // Deliveries timeline (history of fan-out attempts). Loaded lazily
  // because the table can grow large and not every operator wants to
  // see it on first paint. Filter "only failed" is the common forensic
  // use-case so it's a one-click toggle rather than a multi-field form.
  let deliveries = $state<DeliveryRow[]>([]);
  let deliveriesLoading = $state(false);
  let deliveriesLoadingMore = $state(false);
  let deliveriesHasMore = $state(true);
  let deliveriesOnlyFailed = $state(false);
  const DELIVERIES_PAGE = 100;

  // --- Rule add-form state ---

  let ruleName = $state("");
  let ruleEnabled = $state(true);
  const RULE_KINDS = ["track_finalized", "zone_enter", "zone_exit", "zone_dwell"];
  const eventKindLabel = (kind: string) => t(`events_kind_${kind}` as MessageKey);
  let ruleEventKind = $state<string>("track_finalized");
  let ruleCameraIds = $state<string[]>([]);
  let ruleClassIdsRaw = $state(""); // comma-separated, parsed at submit
  let ruleChannelIds = $state<string[]>([]);
  let ruleSaving = $state(false);
  let ruleSaveError = $state<string | null>(null);
  // Currently-selected preset id — for the dropdown's bind:value.
  // Empty string means "no preset chosen / blank form".
  let rulePreset = $state<string>("");

  // Rule form presets. Each maps to a single applyPreset() call that
  // pre-fills the form. New presets land here without backend changes;
  // operator can always tweak the populated form before saving.
  type RulePreset = {
    id: string;
    labelKey: MessageKey;
    name: string;
    event_kind: string;
    class_ids: string;   // raw, comma-separated for the input
  };
  const RULE_PRESETS: RulePreset[] = [
    // 0 = COCO person
    { id: "person_zone",      labelKey: "rules_preset_person_zone",   name: "Osoba ulazi u zonu",        event_kind: "zone_enter",      class_ids: "0" },
    // 2,3,5,7 = car, motorcycle, bus, truck
    { id: "vehicle_zone",     labelKey: "rules_preset_vehicle_zone",  name: "Vozilo ulazi u zonu",       event_kind: "zone_enter",      class_ids: "2,3,5,7" },
    // dwell event fires once after >= dwell_threshold_ms in zone (default 30s)
    { id: "dwell",            labelKey: "rules_preset_dwell",         name: "Zadržavanje u zoni",        event_kind: "zone_dwell",      class_ids: "" },
    { id: "any_finalized",    labelKey: "rules_preset_any_finalized", name: "Bilo koji završen track",   event_kind: "track_finalized", class_ids: "" },
  ];

  function applyPreset(presetId: string) {
    rulePreset = presetId;
    const p = RULE_PRESETS.find((x) => x.id === presetId);
    if (!p) return;
    // Only override fields the preset cares about; cameras + channels
    // stay as the operator left them so they don't lose progress on a
    // preset switch.
    ruleName = p.name;
    ruleEventKind = p.event_kind;
    ruleClassIdsRaw = p.class_ids;
  }

  function channelLabel(id: string): string {
    const c = channels.find((x) => x.id === id);
    if (!c) return id.slice(0, 8);
    return c.target_preview ? `${c.name} (${c.target_preview})` : c.name;
  }

  // Per-row transient state (test send result, save errors).
  let testing = $state<Record<string, boolean>>({});
  let testResult = $state<Record<string, { ok: boolean; error: string | null } | null>>({});

  // --- Add-form state ---

  let formKind = $state<NotificationKind>("webhook");
  let formName = $state("");
  let formTargetPreview = $state("");
  let formEnabled = $state(true);
  let formRateLimit = $state<number>(0);
  // Per-kind form fields. We keep one set of variables per field name
  // and read whichever are relevant for `formKind` at submit time.
  let formWebhookUrl = $state("");
  let formWebhookMethod = $state<"POST" | "PUT">("POST");
  let formSlackUrl = $state("");
  let formTelegramToken = $state("");
  let formTelegramChatId = $state("");
  let formSmtpHost = $state("");
  let formSmtpPort = $state<number>(587);
  let formSmtpUser = $state("");
  let formSmtpPassword = $state("");
  let formSmtpFrom = $state("");
  let formSmtpTo = $state("");
  let formSmtpTls = $state(true);
  let saving = $state(false);
  let saveError = $state<string | null>(null);

  const KINDS: { id: NotificationKind; key: MessageKey }[] = [
    { id: "webhook",  key: "notifications_kind_webhook" },
    { id: "slack",    key: "notifications_kind_slack" },
    { id: "telegram", key: "notifications_kind_telegram" },
    { id: "smtp",     key: "notifications_kind_smtp" },
  ];

  function kindLabel(k: NotificationKind): string {
    const f = KINDS.find((x) => x.id === k);
    return f ? t(f.key) : k;
  }

  async function load() {
    loading = true;
    try {
      // Parallel fetch — three independent calls. Cameras list powers
      // the rule filter dropdown; errors there are non-fatal so the
      // channels/rules editor still works without it.
      const [chs, rls, cams] = await Promise.all([
        api.listNotificationChannels(),
        api.listNotificationRules(),
        api.listCameras().catch(() => [] as Camera[]),
      ]);
      channels = chs;
      rules = rls;
      cameras = cams;
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      loading = false;
    }
  }
  onMount(() => {
    // refreshDeliveries() is owned by the $effect below (runs once on mount);
    // calling it here too fetched the deliveries list twice on first paint.
    load();
  });

  async function refreshDeliveries() {
    deliveriesLoading = true;
    try {
      deliveries = await api.listNotificationDeliveries({
        ok: deliveriesOnlyFailed ? false : undefined,
        limit: DELIVERIES_PAGE,
      });
      deliveriesHasMore = deliveries.length >= DELIVERIES_PAGE;
    } catch (e) {
      console.error("deliveries load failed", e);
    } finally {
      deliveriesLoading = false;
    }
  }

  async function loadMoreDeliveries() {
    if (deliveriesLoadingMore || deliveries.length === 0) return;
    deliveriesLoadingMore = true;
    try {
      const cursor = deliveries[deliveries.length - 1].at;
      const page = await api.listNotificationDeliveries({
        ok: deliveriesOnlyFailed ? false : undefined,
        until: cursor,
        limit: DELIVERIES_PAGE,
      });
      if (page.length === 0) {
        deliveriesHasMore = false;
        return;
      }
      const seen = new Set(deliveries.map((d) => d.id));
      deliveries = [...deliveries, ...page.filter((d) => !seen.has(d.id))];
      deliveriesHasMore = page.length >= DELIVERIES_PAGE;
    } catch (e) {
      console.error("deliveries load more failed", e);
    } finally {
      deliveriesLoadingMore = false;
    }
  }

  $effect(() => {
    deliveriesOnlyFailed;     // dependency
    deliveriesHasMore = true;
    refreshDeliveries();
  });

  function resetRuleForm() {
    ruleName = "";
    ruleEnabled = true;
    ruleEventKind = "track_finalized";
    ruleCameraIds = [];
    ruleClassIdsRaw = "";
    ruleChannelIds = [];
    ruleSaveError = null;
    rulePreset = "";
  }

  function parseClassIds(): number[] {
    const parts = ruleClassIdsRaw
      .split(/[,\s]+/)
      .map((s) => s.trim())
      .filter(Boolean);
    const out: number[] = [];
    for (const p of parts) {
      const n = Number.parseInt(p, 10);
      if (Number.isFinite(n)) out.push(n);
    }
    return out;
  }

  async function submitRule(e: SubmitEvent) {
    e.preventDefault();
    if (ruleChannelIds.length === 0) {
      // Backend doesn't reject empty channel_ids (a rule with no
      // channels is structurally valid, just useless). Guard here so
      // the operator doesn't accidentally make a no-op rule.
      ruleSaveError = t("rules_no_channels_selected");
      return;
    }
    ruleSaving = true;
    ruleSaveError = null;
    const filter: { camera_ids?: string[]; class_ids?: number[] } = {};
    if (ruleCameraIds.length > 0) filter.camera_ids = ruleCameraIds;
    const classIds = parseClassIds();
    if (classIds.length > 0) filter.class_ids = classIds;
    try {
      const created = await api.createNotificationRule({
        name: ruleName,
        enabled: ruleEnabled,
        event_kind: ruleEventKind || null,
        filter,
        channel_ids: ruleChannelIds,
      });
      rules = [...rules, created];
      resetRuleForm();
    } catch (err) {
      ruleSaveError = err instanceof Error ? err.message : String(err);
    } finally {
      ruleSaving = false;
    }
  }

  async function toggleRule(r: NotificationRule) {
    try {
      const updated = await api.patchNotificationRule(r.id, {
        enabled: !r.enabled,
      });
      rules = rules.map((x) => (x.id === r.id ? updated : x));
    } catch (e) {
      await load();
      await dialog.alert({ title: t("rules_section_title"), message: (e as Error).message });
    }
  }

  async function deleteRule(r: NotificationRule) {
    const ok = await dialog.confirm({
      title: t("rules_confirm_delete"),
      message: `"${r.name}"`,
      confirmLabel: t("rules_delete"),
      danger: true,
    });
    if (!ok) return;
    try {
      await api.deleteNotificationRule(r.id);
      rules = rules.filter((x) => x.id !== r.id);
    } catch (e) {
      await dialog.alert({ title: t("rules_confirm_delete"), message: (e as Error).message });
    }
  }

  function resetForm() {
    formName = "";
    formTargetPreview = "";
    formEnabled = true;
    formRateLimit = 0;
    formWebhookUrl = "";
    formWebhookMethod = "POST";
    formSlackUrl = "";
    formTelegramToken = "";
    formTelegramChatId = "";
    formSmtpHost = "";
    formSmtpPort = 587;
    formSmtpUser = "";
    formSmtpPassword = "";
    formSmtpFrom = "";
    formSmtpTo = "";
    formSmtpTls = true;
    saveError = null;
  }

  function buildConfig(): Record<string, unknown> {
    switch (formKind) {
      case "webhook":
        return { url: formWebhookUrl, method: formWebhookMethod };
      case "slack":
        return { url: formSlackUrl };
      case "telegram":
        return { bot_token: formTelegramToken, chat_id: formTelegramChatId };
      case "smtp":
        return {
          host: formSmtpHost,
          port: formSmtpPort,
          username: formSmtpUser || undefined,
          password: formSmtpPassword || undefined,
          from: formSmtpFrom,
          to: formSmtpTo,
          use_tls: formSmtpTls,
        };
    }
  }

  async function submitForm(e: SubmitEvent) {
    e.preventDefault();
    saving = true;
    saveError = null;
    try {
      const created = await api.createNotificationChannel({
        name: formName,
        kind: formKind,
        enabled: formEnabled,
        target_preview: formTargetPreview || undefined,
        rate_limit_per_min: formRateLimit,
        config: buildConfig(),
      });
      channels = [...channels, created];
      resetForm();
    } catch (err) {
      saveError = err instanceof Error ? err.message : String(err);
    } finally {
      saving = false;
    }
  }

  async function toggleEnabled(c: NotificationChannel) {
    try {
      const updated = await api.patchNotificationChannel(c.id, {
        enabled: !c.enabled,
      });
      channels = channels.map((x) => (x.id === c.id ? updated : x));
    } catch (e) {
      // Revert UI on next refresh and surface the failure (fail-loud).
      await load();
      await dialog.alert({ title: t("settings_notifications_title"), message: (e as Error).message });
    }
  }

  async function runTest(c: NotificationChannel) {
    testing = { ...testing, [c.id]: true };
    testResult = { ...testResult, [c.id]: null };
    try {
      const res = await api.testNotificationChannel(c.id);
      testResult = { ...testResult, [c.id]: res };
      // Refresh the row so last_used_at / last_error reflect the test.
      await load();
    } catch (e) {
      testResult = {
        ...testResult,
        [c.id]: { ok: false, error: e instanceof Error ? e.message : String(e) },
      };
    } finally {
      testing = { ...testing, [c.id]: false };
    }
  }

  async function deleteChannel(c: NotificationChannel) {
    const ok = await dialog.confirm({
      title: t("notifications_confirm_delete"),
      message: `"${c.name}" (${kindLabel(c.kind)})`,
      confirmLabel: t("notifications_delete"),
      danger: true,
    });
    if (!ok) return;
    try {
      await api.deleteNotificationChannel(c.id);
      channels = channels.filter((x) => x.id !== c.id);
    } catch (e) {
      await dialog.alert({ title: t("notifications_confirm_delete"), message: (e as Error).message });
    }
  }
</script>

<div class="space-y-6">

  {#if loading}
    <p class="text-m text-baba-text-muted">{t("notifications_loading")}</p>
  {:else if error}
    <p class="text-m text-red-400">{t("cameras_error_prefix")}: {error}</p>
  {:else}
    <!-- Existing channels -->
    {#if channels.length === 0}
      <p class="text-m text-baba-text-faint">{t("notifications_empty")}</p>
    {:else}
      <ul class="divide-y divide-baba-border overflow-hidden rounded-lg border border-baba-border bg-baba-panel">
        {#each channels as c (c.id)}
          {@const tr = testResult[c.id]}
          <li class="flex flex-wrap items-center gap-3 px-4 py-3">
            <div class="min-w-0 flex-1">
              <div class="flex items-baseline gap-2">
                <span class="truncate font-medium">{c.name}</span>
                <Tag tone="quiet">
                  {kindLabel(c.kind)}
                </Tag>
                {#if !c.enabled}
                  <span class="text-s text-baba-text-faint">·</span>
                  <span class="text-s text-red-400">{t("cameras_status_disabled")}</span>
                {/if}
              </div>
              {#if c.target_preview}
                <div class="text-s text-baba-text-faint">{c.target_preview}</div>
              {/if}
              <div class="text-s text-baba-text-faint">
                {t("notifications_rate_limit")}:
                {c.rate_limit_per_min > 0
                  ? `${c.rate_limit_per_min}/min`
                  : t("notifications_rate_unlimited")}
              </div>
              {#if c.last_used_at}
                <div class="text-s text-baba-text-faint">
                  {t("notifications_last_used")}: {dt.short(c.last_used_at)}
                </div>
              {/if}
              {#if c.last_error}
                <div class="text-s text-amber-400" title={c.last_error}>
                  {t("notifications_last_error")}: {c.last_error.slice(0, 80)}
                </div>
              {/if}
              {#if tr}
                <div class="text-s" class:text-emerald-400={tr.ok} class:text-red-400={!tr.ok}>
                  {tr.ok ? t("notifications_test_ok") : `${t("notifications_test_failed")}: ${tr.error ?? ""}`}
                </div>
              {/if}
            </div>
            <label class="flex items-center gap-1 text-s">
              <Toggle size="small" checked={c.enabled} onclick={() => toggleEnabled(c)} />
              {t("notifications_enabled")}
            </label>
            <Button size="small" onclick={() => runTest(c)} disabled={testing[c.id]}>
              {testing[c.id] ? t("notifications_testing") : t("notifications_test")}
            </Button>
            <Button tone="danger" size="small" onclick={() => deleteChannel(c)}>
              {t("notifications_delete")}
            </Button>
          </li>
        {/each}
      </ul>
    {/if}

    <!-- Add form -->
    <section class="rounded-lg border border-baba-border bg-baba-panel">
      <header class="border-b border-baba-border px-4 py-2">
        <h3 class="text-m font-medium">{t("notifications_add_title")}</h3>
      </header>
      <form class="space-y-3 px-4 py-3" onsubmit={submitForm}>
        <div class="grid gap-3 sm:grid-cols-2">
          <label class="flex flex-col gap-1 text-s">
            <span class="text-baba-text-faint">{t("notifications_kind")}</span>
            <select
              bind:value={formKind}
              class="rounded border border-baba-border bg-baba-bg px-2 py-1.5 text-m"
            >
              {#each byLabel(KINDS, (k) => t(k.key)) as k (k.id)}
                <option value={k.id}>{t(k.key)}</option>
              {/each}
            </select>
          </label>

          <label class="flex flex-col gap-1 text-s">
            <span class="text-baba-text-faint">{t("notifications_name")}</span>
            <input
              required
              bind:value={formName}
              class="rounded border border-baba-border bg-baba-bg px-2 py-1.5 text-m"
            />
          </label>
        </div>

        <!-- Per-kind fields -->
        {#if formKind === "webhook"}
          <label class="block text-s">
            <span class="text-baba-text-faint">{t("notifications_field_url")}</span>
            <input
              required
              type="url"
              bind:value={formWebhookUrl}
              class="mt-1 w-full rounded border border-baba-border bg-baba-bg px-2 py-1.5 text-m"
            />
            <p class="mt-1 text-baba-text-faint">{t("notifications_field_url_hint_webhook")}</p>
          </label>
          <label class="block text-s">
            <span class="text-baba-text-faint">{t("notifications_field_method")}</span>
            <select
              bind:value={formWebhookMethod}
              class="mt-1 rounded border border-baba-border bg-baba-bg px-2 py-1.5 text-m"
            >
              <option value="POST">POST</option>
              <option value="PUT">PUT</option>
            </select>
          </label>
        {:else if formKind === "slack"}
          <label class="block text-s">
            <span class="text-baba-text-faint">{t("notifications_field_url")}</span>
            <input
              required
              type="url"
              placeholder="https://hooks.slack.com/services/..."
              bind:value={formSlackUrl}
              class="mt-1 w-full rounded border border-baba-border bg-baba-bg px-2 py-1.5 text-m"
            />
            <p class="mt-1 text-baba-text-faint">{t("notifications_field_url_hint_slack")}</p>
          </label>
        {:else if formKind === "telegram"}
          <div class="grid gap-3 sm:grid-cols-2">
            <label class="block text-s">
              <span class="text-baba-text-faint">{t("notifications_field_bot_token")}</span>
              <input
                required
                bind:value={formTelegramToken}
                class="mt-1 w-full rounded border border-baba-border bg-baba-bg px-2 py-1.5 font-mono text-m"
              />
            </label>
            <label class="block text-s">
              <span class="text-baba-text-faint">{t("notifications_field_chat_id")}</span>
              <input
                required
                bind:value={formTelegramChatId}
                class="mt-1 w-full rounded border border-baba-border bg-baba-bg px-2 py-1.5 font-mono text-m"
              />
            </label>
          </div>
        {:else if formKind === "smtp"}
          <div class="grid gap-3 sm:grid-cols-2">
            <label class="block text-s">
              <span class="text-baba-text-faint">{t("notifications_field_smtp_host")}</span>
              <input
                required
                bind:value={formSmtpHost}
                class="mt-1 w-full rounded border border-baba-border bg-baba-bg px-2 py-1.5 text-m"
              />
            </label>
            <label class="block text-s">
              <span class="text-baba-text-faint">{t("notifications_field_smtp_port")}</span>
              <input
                type="number" min="1" max="65535"
                bind:value={formSmtpPort}
                class="mt-1 w-full rounded border border-baba-border bg-baba-bg px-2 py-1.5 text-m"
              />
            </label>
            <label class="block text-s">
              <span class="text-baba-text-faint">{t("notifications_field_smtp_user")}</span>
              <input
                bind:value={formSmtpUser}
                class="mt-1 w-full rounded border border-baba-border bg-baba-bg px-2 py-1.5 text-m"
              />
            </label>
            <label class="block text-s">
              <span class="text-baba-text-faint">{t("notifications_field_smtp_password")}</span>
              <input
                type="password"
                bind:value={formSmtpPassword}
                class="mt-1 w-full rounded border border-baba-border bg-baba-bg px-2 py-1.5 text-m"
              />
            </label>
            <label class="block text-s">
              <span class="text-baba-text-faint">{t("notifications_field_smtp_from")}</span>
              <input
                required
                type="email"
                bind:value={formSmtpFrom}
                class="mt-1 w-full rounded border border-baba-border bg-baba-bg px-2 py-1.5 text-m"
              />
            </label>
            <label class="block text-s">
              <span class="text-baba-text-faint">{t("notifications_field_smtp_to")}</span>
              <input
                required
                type="email"
                bind:value={formSmtpTo}
                class="mt-1 w-full rounded border border-baba-border bg-baba-bg px-2 py-1.5 text-m"
              />
            </label>
          </div>
          <label class="flex items-center gap-2 text-s">
            <input type="checkbox" bind:checked={formSmtpTls} />
            {t("notifications_field_smtp_tls")}
          </label>
        {/if}

        <div class="grid gap-3 sm:grid-cols-2">
          <label class="flex flex-col gap-1 text-s">
            <span class="text-baba-text-faint">{t("notifications_target_preview")}</span>
            <input
              bind:value={formTargetPreview}
              class="rounded border border-baba-border bg-baba-bg px-2 py-1.5 text-m"
            />
            <p class="text-baba-text-faint">{t("notifications_target_preview_hint")}</p>
          </label>
          <label class="flex flex-col gap-1 text-s">
            <span class="text-baba-text-faint">{t("notifications_rate_limit")}</span>
            <input
              type="number"
              min="0"
              max="100000"
              bind:value={formRateLimit}
              class="rounded border border-baba-border bg-baba-bg px-2 py-1.5 text-m tabular-nums"
            />
            <p class="text-baba-text-faint">{t("notifications_rate_limit_hint")}</p>
          </label>
        </div>

        <label class="flex items-center gap-2 text-s">
          <input type="checkbox" bind:checked={formEnabled} />
          {t("notifications_enabled")}
        </label>

        {#if saveError}
          <p class="text-m text-red-400">{saveError}</p>
        {/if}

        <Button tone="primary" type="submit" disabled={saving}>
          {saving ? t("notifications_saving") : t("notifications_save")}
        </Button>
      </form>
    </section>

    <!-- ============================================================ -->
    <!-- Rules section: bind events to channels                       -->
    <!-- ============================================================ -->
    <header class="border-t border-baba-border pt-6">
      <h3 class="text-xl font-semibold">{t("rules_section_title")}</h3>
      <p class="mt-1 text-m text-baba-text-muted">{t("rules_section_desc")}</p>
    </header>

    {#if rules.length === 0}
      <p class="text-m text-baba-text-faint">{t("rules_empty")}</p>
    {:else}
      <ul class="divide-y divide-baba-border overflow-hidden rounded-lg border border-baba-border bg-baba-panel">
        {#each rules as r (r.id)}
          <li class="flex flex-wrap items-center gap-3 px-4 py-3">
            <div class="min-w-0 flex-1">
              <div class="flex items-baseline gap-2">
                <span class="truncate font-medium">{r.name}</span>
                <Tag tone="quiet">
                  {r.event_kind ? eventKindLabel(r.event_kind) : t("rules_event_kind_all")}
                </Tag>
                {#if !r.enabled}
                  <span class="text-s text-baba-text-faint">·</span>
                  <span class="text-s text-red-400">{t("cameras_status_disabled")}</span>
                {/if}
              </div>
              <div class="text-s text-baba-text-faint">
                {#if r.channel_ids.length === 0}
                  {t("rules_no_channels_selected")}
                {:else}
                  → {r.channel_ids.map((id) => channelLabel(id)).join(", ")}
                {/if}
              </div>
              {#if r.filter.camera_ids && r.filter.camera_ids.length > 0}
                <div class="text-s text-baba-text-faint">
                  {t("rules_filter_cameras")}: {r.filter.camera_ids.length}
                </div>
              {/if}
              {#if r.filter.class_ids && r.filter.class_ids.length > 0}
                <div class="text-s text-baba-text-faint">
                  {t("rules_filter_classes")}: {r.filter.class_ids.join(", ")}
                </div>
              {/if}
              {#if r.last_fired_at}
                <div class="text-s text-baba-text-faint">
                  {t("rules_last_fired")}: {dt.short(r.last_fired_at)}
                </div>
              {/if}
              {#if r.last_error}
                <div class="text-s text-amber-400" title={r.last_error}>
                  {t("rules_last_error")}: {r.last_error.slice(0, 100)}
                </div>
              {/if}
            </div>
            <label class="flex items-center gap-1 text-s">
              <Toggle size="small" checked={r.enabled} onclick={() => toggleRule(r)} />
              {t("rules_enabled")}
            </label>
            <Button tone="danger" size="small" onclick={() => deleteRule(r)}>
              {t("rules_delete")}
            </Button>
          </li>
        {/each}
      </ul>
    {/if}

    <section class="rounded-lg border border-baba-border bg-baba-panel">
      <header class="border-b border-baba-border px-4 py-2">
        <h3 class="text-m font-medium">{t("rules_add_title")}</h3>
      </header>
      {#if channels.length === 0}
        <p class="px-4 py-3 text-m text-baba-text-faint">
          {t("rules_no_channels_yet")}
        </p>
      {:else}
        <form class="space-y-3 px-4 py-3" onsubmit={submitRule}>
          <!-- Preset dropdown — pre-fills name + event_kind + class_ids
               in one click. Cameras + channels are kept untouched so an
               operator who's just picking channels doesn't lose progress
               when reaching for a preset. -->
          <label class="block text-s">
            <span class="text-baba-text-faint">{t("rules_preset_label")}</span>
            <select
              value={rulePreset}
              onchange={(e) => applyPreset((e.currentTarget as HTMLSelectElement).value)}
              class="mt-1 rounded border border-baba-border bg-baba-bg px-2 py-1.5 text-m"
            >
              <option value="">{t("rules_preset_blank")}</option>
              {#each RULE_PRESETS as p (p.id)}
                <option value={p.id}>{t(p.labelKey)}</option>
              {/each}
            </select>
          </label>

          <div class="grid gap-3 sm:grid-cols-2">
            <label class="flex flex-col gap-1 text-s">
              <span class="text-baba-text-faint">{t("rules_name")}</span>
              <input
                required
                bind:value={ruleName}
                class="rounded border border-baba-border bg-baba-bg px-2 py-1.5 text-m"
              />
            </label>

            <label class="flex flex-col gap-1 text-s">
              <span class="text-baba-text-faint">{t("rules_event_kind")}</span>
              <select
                bind:value={ruleEventKind}
                class="rounded border border-baba-border bg-baba-bg px-2 py-1.5 text-m"
              >
                {#each byLabel(RULE_KINDS, eventKindLabel) as kind (kind)}
                  <option value={kind}>{eventKindLabel(kind)}</option>
                {/each}
                <option value="">{t("rules_event_kind_all")}</option>
              </select>
            </label>
          </div>

          <label class="block text-s">
            <span class="text-baba-text-faint">{t("rules_filter_cameras")}</span>
            <select
              multiple
              bind:value={ruleCameraIds}
              size={Math.min(cameras.length || 1, 5)}
              class="mt-1 w-full rounded border border-baba-border bg-baba-bg px-2 py-1.5 text-m"
            >
              {#each byLabel(cameras, (c) => c.name) as cam (cam.id)}
                <option value={cam.id}>{cam.name}</option>
              {/each}
            </select>
            <p class="mt-1 text-baba-text-faint">{t("rules_filter_cameras_hint")}</p>
          </label>

          <label class="block text-s">
            <span class="text-baba-text-faint">{t("rules_filter_classes")}</span>
            <input
              bind:value={ruleClassIdsRaw}
              placeholder="0, 2, 7"
              class="mt-1 w-full rounded border border-baba-border bg-baba-bg px-2 py-1.5 font-mono text-m"
            />
            <p class="mt-1 text-baba-text-faint">{t("rules_filter_classes_hint")}</p>
          </label>

          <label class="block text-s">
            <span class="text-baba-text-faint">{t("rules_channels")}</span>
            <select
              multiple
              bind:value={ruleChannelIds}
              size={Math.min(channels.length, 5)}
              class="mt-1 w-full rounded border border-baba-border bg-baba-bg px-2 py-1.5 text-m"
            >
              {#each byLabel(channels, (c) => c.name) as c (c.id)}
                <option value={c.id}>{c.name} ({kindLabel(c.kind)})</option>
              {/each}
            </select>
            <p class="mt-1 text-baba-text-faint">{t("rules_channels_hint")}</p>
          </label>

          <label class="flex items-center gap-2 text-s">
            <input type="checkbox" bind:checked={ruleEnabled} />
            {t("rules_enabled")}
          </label>

          {#if ruleSaveError}
            <p class="text-m text-red-400">{ruleSaveError}</p>
          {/if}

          <Button tone="primary" type="submit" disabled={ruleSaving}>
            {ruleSaving ? t("rules_saving") : t("rules_save")}
          </Button>
        </form>
      {/if}
    </section>

    <!-- ============================================================ -->
    <!-- Deliveries history (per-fire audit)                          -->
    <!-- ============================================================ -->
    <header class="border-t border-baba-border pt-6">
      <h3 class="text-xl font-semibold">{t("deliveries_section_title")}</h3>
      <p class="mt-1 text-m text-baba-text-muted">{t("deliveries_section_desc")}</p>
    </header>

    <div class="flex items-center gap-3">
      <label class="flex items-center gap-2 text-s">
        <Toggle size="small" checked={deliveriesOnlyFailed} onclick={() => (deliveriesOnlyFailed = !deliveriesOnlyFailed)} />
        {t("deliveries_filter_only_failed")}
      </label>
    </div>

    {#if deliveriesLoading}
      <p class="text-m text-baba-text-muted">{t("deliveries_loading")}</p>
    {:else if deliveries.length === 0}
      <p class="text-m text-baba-text-faint">{t("deliveries_empty")}</p>
    {:else}
      <div class="overflow-x-auto rounded-lg border border-baba-border bg-baba-panel">
        <table class="w-full text-m">
          <thead class="bg-baba-panel-2 text-s uppercase tracking-wide text-baba-text-faint">
            <tr>
              <th class="px-3 py-2 text-left">{t("deliveries_col_when")}</th>
              <th class="px-3 py-2 text-left">{t("deliveries_col_rule")}</th>
              <th class="px-3 py-2 text-left">{t("deliveries_col_channel")}</th>
              <th class="px-3 py-2 text-left">{t("deliveries_col_event")}</th>
              <th class="px-3 py-2 text-left">{t("deliveries_col_status")}</th>
              <th class="px-3 py-2 text-right">{t("deliveries_col_duration")}</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-baba-border">
            {#each deliveries as d (d.id)}
              <tr>
                <td class="whitespace-nowrap px-3 py-2 text-s text-baba-text-muted" title={dt.full(d.at)}>
                  {dt.short(d.at)}
                </td>
                <td class="px-3 py-2 text-s">
                  {#if d.rule_name}
                    {d.rule_name}
                  {:else}
                    <span class="text-baba-text-faint italic">{t("deliveries_deleted_rule")}</span>
                  {/if}
                </td>
                <td class="px-3 py-2 text-s">
                  {#if d.channel_name}
                    {d.channel_name}
                  {:else}
                    <span class="text-baba-text-faint italic">{t("deliveries_deleted_channel")}</span>
                  {/if}
                  <span class="ml-1"><Tag tone="quiet">
                    {d.channel_kind}
                  </Tag></span>
                </td>
                <td class="px-3 py-2 text-s">{eventKindLabel(d.event_kind)}</td>
                <td class="px-3 py-2">
                  {#if d.ok}
                    <Tag tone="ok">
                      {t("deliveries_status_ok")}
                    </Tag>
                  {:else}
                    <Tag tone="err" title={d.error ?? ""}>
                      {t("deliveries_status_fail")}
                    </Tag>
                    {#if d.error}
                      <span class="ml-1 text-s text-baba-text-faint">{d.error.slice(0, 60)}</span>
                    {/if}
                  {/if}
                </td>
                <td class="whitespace-nowrap px-3 py-2 text-right tabular-nums text-s text-baba-text-muted">
                  {d.duration_ms != null ? `${d.duration_ms} ms` : "—"}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>

      <div class="text-center">
        {#if deliveriesHasMore}
          <Button onclick={loadMoreDeliveries} disabled={deliveriesLoadingMore}>
            {deliveriesLoadingMore ? t("events_loading_more") : t("deliveries_load_more")}
          </Button>
        {:else}
          <p class="text-s text-baba-text-faint">{t("deliveries_end_of_list")}</p>
        {/if}
      </div>
    {/if}
  {/if}
</div>
