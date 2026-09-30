<script lang="ts">
  import { Tag } from "$lib/kit";
  import { onMount } from "svelte";
  import { api } from "$lib/api";
  import { t, type MessageKey } from "$lib/i18n";
  // Each card represents one onboarding step + how we know it's done.
  // Done = a single derived boolean per card, recomputed when the
  // underlying data loads. We deliberately don't persist a separate
  // "dismissed" flag — the data is the source of truth.
  type Step = {
    titleKey: MessageKey;
    descKey: MessageKey;
    ctaKey: MessageKey;
    href: string;
    done: boolean;
  };

  let loading = $state(true);
  let cameraCount = $state<number>(0);
  let aiConfigured = $state<boolean>(false);
  let notificationsReady = $state<boolean>(false);
  let twoFaEnabled = $state<boolean>(false);

  async function load() {
    loading = true;
    try {
      const [stats, ai, channels, rules, totp] = await Promise.all([
        api.getStats().catch(() => null),
        api.listAiSettings().catch(() => []),
        api.listNotificationChannels().catch(() => []),
        api.listNotificationRules().catch(() => []),
        api.totpStatus().catch(() => ({ enabled: false, enrolling: false })),
      ]);
      cameraCount = stats?.cameras_total ?? 0;
      aiConfigured = ai.some((s) => s.enabled && !!s.api_key_masked);
      // "Notifications ready" = at least one channel AND at least one
      // rule that references at least one of those channels. The strict
      // version would walk every rule's channel_ids vs the channel list;
      // for the wizard a coarse check (any channel + any rule) is
      // enough operator signal.
      notificationsReady = channels.length > 0 && rules.length > 0;
      twoFaEnabled = !!totp.enabled;
    } finally {
      loading = false;
    }
  }
  onMount(load);

  const steps = $derived<Step[]>([
    {
      titleKey: "onboarding_cameras_title",
      descKey: "onboarding_cameras_desc",
      ctaKey: "onboarding_cameras_cta",
      href: "/settings/cameras",
      done: cameraCount > 0,
    },
    {
      titleKey: "onboarding_ai_title",
      descKey: "onboarding_ai_desc",
      ctaKey: "onboarding_ai_cta",
      href: "/settings/ai",
      done: aiConfigured,
    },
    {
      titleKey: "onboarding_notif_title",
      descKey: "onboarding_notif_desc",
      ctaKey: "onboarding_notif_cta",
      href: "/settings/notifications",
      done: notificationsReady,
    },
    {
      titleKey: "onboarding_2fa_title",
      descKey: "onboarding_2fa_desc",
      ctaKey: "onboarding_2fa_cta",
      href: "/account",
      done: twoFaEnabled,
    },
  ]);

  const completedCount = $derived(steps.filter((s) => s.done).length);
</script>

<div class="space-y-6 max-w-3xl">

  {#if loading}
    <p class="text-m text-baba-text-muted">{t("dashboard_loading")}</p>
  {:else}
    <!-- Progress strip. Small but useful — operator sees "2/4 done"
         and can tell at a glance how far they are. -->
    <div class="flex items-center gap-3 text-m">
      <div class="flex-1 h-2 overflow-hidden rounded-full bg-baba-panel-2">
        <div
          class="h-full bg-baba-accent transition-all"
          style="width: {(completedCount / steps.length) * 100}%"
        ></div>
      </div>
      <span class="tabular-nums text-baba-text-muted">{completedCount}/{steps.length}</span>
    </div>

    <ul class="space-y-3">
      {#each steps as s (s.titleKey)}
        <li class="rounded-lg border border-baba-border bg-baba-panel p-4">
          <div class="flex items-start justify-between gap-4">
            <div class="min-w-0 flex-1">
              <div class="flex items-baseline gap-2">
                <h3 class="font-medium">{t(s.titleKey)}</h3>
                {#if s.done}
                  <Tag tone="ok">
                    {t("onboarding_step_done")}
                  </Tag>
                {:else}
                  <Tag tone="quiet">
                    {t("onboarding_step_todo")}
                  </Tag>
                {/if}
              </div>
              <p class="mt-1 text-m text-baba-text-muted">{t(s.descKey)}</p>
            </div>
            <a
              href={s.href}
              class="shrink-0 rounded border border-baba-border bg-baba-panel-2 px-3 py-1.5 text-s hover:bg-baba-panel"
            >{t(s.ctaKey)}</a>
          </div>
        </li>
      {/each}
    </ul>
  {/if}
</div>
