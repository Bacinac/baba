<script lang="ts">
  import { onMount } from "svelte";
  import { auth } from "$lib/auth.svelte";
  import { Button, Card, Field, i18n, Notice, theme, toasts, type Locale, type Theme, Tag, SaveButton } from "$lib/kit";
  import { t, type MessageKey } from "$lib/i18n";
  import { api, type UserPreferences } from "$lib/api";
  import { ApiError } from "$lib/kit";
  import { dt } from "$lib/datetime.svelte";

  let createdAt = $state<string | null>(null);
  let lastLoginAt = $state<string | null>(null);

  // password
  let oldPw = $state("");
  let newPw = $state("");
  let confirmPw = $state("");
  let pwBusy = $state(false);
  let pwError = $state<string | null>(null);

  // preferences
  let prefLanding = $state<string>("");
  let prefTimeFormat = $state<"auto" | "24" | "12">("auto");
  let prefTimezone = $state<string>("");
  let prefTheme = $state<Theme>("dark");
  let prefLocale = $state<Locale>("hr");
  let prefBusy = $state(false);
  let prefError = $state<string | null>(null);
  let prefStored = $state("");
  const prefDirty = $derived(JSON.stringify(prefPatch()) !== prefStored);

  function prefPatch(): UserPreferences {
    return {
      default_landing: prefLanding || null,
      time_format_24h: prefTimeFormat === "auto" ? null : prefTimeFormat === "24",
      timezone: prefTimezone || null,
      theme: prefTheme,
      locale: prefLocale,
    };
  }

  const LANDING_OPTIONS: { value: string; key: MessageKey | null }[] = [
    { value: "",            key: null },
    { value: "/live",       key: "nav_live" },
    { value: "/activity",   key: "nav_sightings" },
    { value: "/identities", key: "nav_identities" },
    { value: "/analytics",  key: "nav_analytics" },
    { value: "/search",     key: "nav_search" },
  ];
  const TIME_FORMATS: { v: "auto" | "24" | "12"; k: MessageKey }[] = [
    { v: "auto", k: "pref_time_locale" },
    { v: "24",   k: "pref_time_24h" },
    { v: "12",   k: "pref_time_12h" },
  ];
  const THEME_OPTIONS: { v: Theme; k: MessageKey }[] = [
    { v: "dark",   k: "theme_dark" },
    { v: "light",  k: "theme_light" },
    { v: "system", k: "theme_system" },
  ];
  const LOCALE_OPTIONS: { v: Locale; k: MessageKey }[] = [
    { v: "hr", k: "pref_language_hr" },
    { v: "en", k: "pref_language_en" },
  ];
  const TZ_COMMON = [
    "Europe/Zagreb", "Europe/Vienna", "Europe/Berlin", "Europe/London",
    "Europe/Madrid", "Europe/Athens", "UTC", "America/New_York",
    "America/Chicago", "America/Los_Angeles",
  ];

  onMount(async () => {
    if (!auth.user) return;
    try {
      const u = await api.getUser(auth.user.id);
      createdAt = u.created_at ?? null;
      lastLoginAt = u.last_login_at ?? null;
    } catch { /* ignore */ }
    try {
      const p: UserPreferences = await api.getPreferences();
      prefLanding = p.default_landing ?? "";
      prefTimeFormat = p.time_format_24h === true ? "24" : p.time_format_24h === false ? "12" : "auto";
      prefTimezone = p.timezone ?? "";
      // Reflect whatever's currently active: server value if set, otherwise
      // the local store state (which already incorporates localStorage).
      prefTheme = (p.theme as Theme | undefined) ?? theme.theme;
      prefLocale = (p.locale as Locale | undefined) ?? i18n.locale;
      prefStored = JSON.stringify(prefPatch());
    } catch { /* ignore */ }
  });

  async function changePassword(e: SubmitEvent) {
    e.preventDefault();
    pwError = null;
    if (newPw !== confirmPw) { pwError = t("users_password_mismatch"); return; }
    pwBusy = true;
    const result = await auth.changePassword(oldPw, newPw);
    pwBusy = false;
    if (result === null) {
      toasts.success(t("users_change_password_done"));
      oldPw = ""; newPw = ""; confirmPw = "";
      return;
    }
    pwError =
      result === "wrong" ? t("users_change_password_wrong") :
      result === "weak"  ? t("users_password_min") :
      t("login_error_network");
  }

  async function savePreferences(e: SubmitEvent) {
    e.preventDefault();
    prefBusy = true; prefError = null;
    try {
      const patch = prefPatch();
      const saved = await api.patchPreferences(patch);
      dt.setPrefs(saved);
      prefStored = JSON.stringify(patch);
      // Apply theme/locale immediately so the page reflects the save without
      // needing a reload. setTheme/setLocale also update localStorage so FOUC
      // prevention picks the same value on next load.
      theme.setTheme(prefTheme);
      i18n.set(prefLocale);
      toasts.success(t("pref_saved"));
    } catch (err) {
      // Was silently swallowed (unhandled rejection) — every other form on
      // this page surfaces its failure, so this one should too.
      prefError = err instanceof Error ? err.message : String(err);
    } finally {
      prefBusy = false;
    }
  }

  function landingLabel(opt: { value: string; key: MessageKey | null }): string {
    return opt.key === null ? t("pref_default_landing_auto") : t(opt.key);
  }

  // --- 2FA TOTP ---

  let totpState = $state<"enabled" | "disabled" | "enrolling" | "loading" | "unknown">("loading");
  let totpSetupSecret = $state<string>("");
  let totpConfirmCode = $state<string>("");
  let totpBusy = $state(false);
  let totpError = $state<string | null>(null);
  let totpDisablePassword = $state<string>("");
  // Cache-bust the QR <img src> on every fresh enrollment so the
  // browser doesn't show a stale QR for a previous secret.
  let totpQrBust = $state<number>(0);

  // Recovery codes — shown once at enable / regenerate and then
  // explicitly acknowledged ("I've saved them") so the operator can't
  // miss the warning. Backend never returns them again.
  let recoveryCodes = $state<string[] | null>(null);
  let recoveryCopied = $state(false);
  // Regenerate UI (only relevant after enrollment is done)
  let regenPassword = $state<string>("");
  let regenBusy = $state(false);

  async function refreshTotpStatus() {
    try {
      const s = await api.totpStatus();
      totpState = s.enabled ? "enabled" : s.enrolling ? "enrolling" : "disabled";
    } catch {
      // Not the same as off. Reporting a failed status call as "2FA
      // isključena" tells the operator their account is unprotected when what
      // actually happened is that we could not find out — and the template had
      // no branch for the initial "loading" either, so the definitive badge
      // rendered before the answer arrived.
      totpState = "unknown";
    }
  }

  async function startTotpSetup() {
    totpBusy = true; totpError = null;
    try {
      const r = await api.totpSetup();
      totpSetupSecret = r.secret;
      totpState = "enrolling";
      totpQrBust = Date.now();
    } catch (e) {
      totpError = e instanceof Error ? e.message : String(e);
    } finally {
      totpBusy = false;
    }
  }

  async function confirmTotp(e: SubmitEvent) {
    e.preventDefault();
    totpBusy = true; totpError = null;
    try {
      const r = await api.totpEnable(totpConfirmCode.trim());
      toasts.success(t("account_2fa_done"));
      totpConfirmCode = "";
      totpSetupSecret = "";
      // Surface the new recovery codes — operator MUST copy them now.
      // The UI stays in the "recovery shown" state until they tick the
      // ack button, then we move to the normal enabled view.
      recoveryCodes = r.codes;
      recoveryCopied = false;
      await refreshTotpStatus();
    } catch (err) {
      // 401 from /enable means the code was wrong.
      totpError = err instanceof ApiError && err.status === 401
        ? t("account_2fa_error_wrong_code")
        : (err as Error).message;
    } finally {
      totpBusy = false;
    }
  }

  async function copyRecoveryCodes() {
    if (!recoveryCodes) return;
    try {
      await navigator.clipboard.writeText(recoveryCodes.join("\n"));
      recoveryCopied = true;
      setTimeout(() => (recoveryCopied = false), 2000);
    } catch {
      // navigator.clipboard requires HTTPS / localhost; operators on
      // plain HTTP fall back to the download button below.
    }
  }

  function downloadRecoveryCodes() {
    if (!recoveryCodes) return;
    const body =
      "BABA 2FA recovery codes\n" +
      `Account: ${auth.user?.username ?? "?"}\n` +
      `Generated: ${new Date().toISOString()}\n\n` +
      recoveryCodes.join("\n") +
      "\n\n" +
      "Each code can be used once instead of your authenticator's 6-digit code.\n";
    const blob = new Blob([body], { type: "text/plain" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `baba-2fa-recovery-${auth.user?.username ?? "user"}.txt`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }

  async function regenerateCodes(e: SubmitEvent) {
    e.preventDefault();
    regenBusy = true; totpError = null;
    try {
      const r = await api.totpRegenerateRecovery(regenPassword);
      recoveryCodes = r.codes;
      recoveryCopied = false;
      regenPassword = "";
    } catch (err) {
      totpError = err instanceof ApiError && err.status === 401
        ? t("account_2fa_error_wrong_password")
        : (err as Error).message;
    } finally {
      regenBusy = false;
    }
  }

  async function cancelEnrollment(e: SubmitEvent) {
    // Cancel mid-enrollment = ask backend to disable (wipes the
    // unconfirmed secret) without verifying anything. Reuses the
    // disable endpoint; the password requirement still applies.
    e.preventDefault();
    if (!totpDisablePassword) return;
    await disableTotp(e);
  }

  async function disableTotp(e: SubmitEvent) {
    e.preventDefault();
    totpBusy = true; totpError = null;
    try {
      await api.totpDisable(totpDisablePassword);
      toasts.success(t("account_2fa_disabled_done"));
      totpDisablePassword = "";
      totpSetupSecret = "";
      await refreshTotpStatus();
    } catch (err) {
      totpError = err instanceof ApiError && err.status === 401
        ? t("account_2fa_error_wrong_password")
        : (err as Error).message;
    } finally {
      totpBusy = false;
    }
  }

  onMount(refreshTotpStatus);
</script>

<div class="flex w-full max-w-4xl flex-col gap-6">

  <Card title={t("account_section_profile")}>
      <dl class="grid grid-cols-1 gap-x-6 gap-y-2 text-m sm:grid-cols-2 lg:grid-cols-4">
        <div>
          <dt class="text-s text-baba-text-faint">{t("account_field_username")}</dt>
          <dd class="font-mono">{auth.user?.username ?? "—"}</dd>
        </div>
        <div>
          <dt class="text-s text-baba-text-faint">{t("account_field_role")}</dt>
          <dd>{auth.user?.role ?? "—"}</dd>
        </div>
        <div>
          <dt class="text-s text-baba-text-faint">{t("account_field_created")}</dt>
          <dd>{dt.full(createdAt)}</dd>
        </div>
        <div>
          <dt class="text-s text-baba-text-faint">{t("account_field_last_login")}</dt>
          <dd>{dt.full(lastLoginAt)}</dd>
        </div>
      </dl>
  </Card>

  <Card title={t("account_section_password")}>
      <form onsubmit={changePassword} class="space-y-3">
        <div class="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <Field label={t("users_password_current")} id="pw-cur">
            <input id="pw-cur" type="password" required bind:value={oldPw}
              autocomplete="current-password" class="w-full" />
          </Field>
          <Field label={t("users_password_new")} id="pw-new" hint={t("users_password_min")}>
            <input id="pw-new" type="password" required minlength="8" bind:value={newPw}
              autocomplete="new-password" class="w-full" />
          </Field>
          <Field label={t("users_password_confirm")} id="pw-conf">
            <input id="pw-conf" type="password" required minlength="8" bind:value={confirmPw}
              autocomplete="new-password" class="w-full" />
          </Field>
        </div>
        {#if pwError}<Notice tone="err">{pwError}</Notice>{/if}
        <Button tone="primary" type="submit" disabled={pwBusy}>
          {pwBusy ? t("users_saving") : t("users_save")}
        </Button>
      </form>
  </Card>

  <Card title={t("account_section_2fa")}>
      <div class="space-y-3">
        <div class="flex items-center gap-2 text-m">
          {#if totpState === "enabled"}
            <Tag tone="ok">
              ● {t("account_2fa_status_enabled")}
            </Tag>
          {:else if totpState === "enrolling"}
            <Tag tone="warn">
              ● {t("account_2fa_status_enrolling")}
            </Tag>
          {:else if totpState === "disabled"}
            <Tag tone="quiet">
              {t("account_2fa_status_disabled")}
            </Tag>
          {:else if totpState === "unknown"}
            <Tag tone="warn">
              ⚠ {t("account_2fa_status_unknown")}
            </Tag>
          {/if}
        </div>

        {#if totpState === "disabled"}
          <p class="text-m text-baba-text-muted">{t("account_2fa_intro_disabled")}</p>
          <Button tone="primary" onclick={startTotpSetup} disabled={totpBusy}>
            {t("account_2fa_setup")}
          </Button>
        {:else if totpState === "enrolling"}
          <p class="text-m text-baba-text-muted">{t("account_2fa_intro_enrolling")}</p>
          <div class="grid gap-4 sm:grid-cols-[auto_1fr]">
            <div class="rounded border border-baba-border bg-white p-2">
              <img
                src={`${api.totpQrUrl()}?_=${totpQrBust}`}
                alt="2FA QR"
                class="block h-48 w-48"
              />
            </div>
            <div class="space-y-3">
              <Field label={t("account_2fa_secret_label")}>
                <input value={totpSetupSecret} readonly class="w-full font-mono" />
              </Field>
              <form onsubmit={confirmTotp} class="space-y-2">
                <Field label={t("account_2fa_confirm_label")} id="totp-confirm">
                  <input
                    id="totp-confirm"
                    inputmode="numeric"
                    autocomplete="one-time-code"
                    maxlength="8"
                    required
                    bind:value={totpConfirmCode}
                    class="w-full font-mono"
                  />
                </Field>
                <div class="flex flex-wrap gap-2">
                  <Button tone="primary" type="submit" disabled={totpBusy}>
                    {totpBusy ? t("account_2fa_confirming") : t("account_2fa_confirm")}
                  </Button>
                </div>
              </form>
            </div>
          </div>

          <!-- Cancellation requires the password (same path as Disable below). -->
          <form onsubmit={cancelEnrollment} class="space-y-2 border-t border-baba-border pt-3">
            <Field label={t("account_2fa_disable_password")} id="totp-cancel-pw">
              <input
                id="totp-cancel-pw"
                type="password"
                autocomplete="current-password"
                required
                bind:value={totpDisablePassword}
                class="w-full"
              />
            </Field>
            <Button tone="danger" type="submit" disabled={totpBusy}>
              {t("account_2fa_cancel_enrollment")}
            </Button>
          </form>
        {:else if totpState === "enabled"}
          <p class="text-m text-baba-text-muted">{t("account_2fa_intro_enabled")}</p>
          <form onsubmit={disableTotp} class="space-y-2">
            <Field label={t("account_2fa_disable_password")} id="totp-disable-pw">
              <input
                id="totp-disable-pw"
                type="password"
                autocomplete="current-password"
                required
                bind:value={totpDisablePassword}
                class="w-full"
              />
            </Field>
            <Button tone="danger" type="submit" disabled={totpBusy}>
              {totpBusy ? t("account_2fa_disabling") : t("account_2fa_disable")}
            </Button>
          </form>

          <!-- Regenerate recovery codes — useful after one was used
               (set is one short) or if the printed copy was lost. -->
          <form onsubmit={regenerateCodes} class="space-y-2 border-t border-baba-border pt-3">
            <div>
              <div class="text-m font-medium">{t("account_2fa_regenerate_title")}</div>
              <p class="mt-1 text-s text-baba-text-muted">{t("account_2fa_regenerate_desc")}</p>
            </div>
            <Field label={t("account_2fa_disable_password")} id="totp-regen-pw">
              <input
                id="totp-regen-pw"
                type="password"
                autocomplete="current-password"
                required
                bind:value={regenPassword}
                class="w-full"
              />
            </Field>
            <Button tone="primary" type="submit" disabled={regenBusy}>
              {regenBusy ? t("account_2fa_regenerating") : t("account_2fa_regenerate")}
            </Button>
          </form>
        {/if}

        {#if totpError}<Notice tone="err">{totpError}</Notice>{/if}

        <!-- Recovery codes panel — shown once per generation. Operator
             must explicitly acknowledge via the "I've saved them"
             button to dismiss; the codes will never be retrievable
             again. -->
        {#if recoveryCodes}
          <div class="rounded-lg border border-amber-500/40 bg-amber-500/10 p-4 space-y-3">
            <div>
              <div class="text-m font-medium">{t("account_2fa_recovery_title")}</div>
              <p class="mt-1 text-s text-baba-text-muted">{t("account_2fa_recovery_intro")}</p>
            </div>
            <div class="grid grid-cols-2 gap-2 sm:grid-cols-5">
              {#each recoveryCodes as code (code)}
                <div class="rounded bg-baba-bg px-2 py-1.5 text-center font-mono text-m">
                  {code}
                </div>
              {/each}
            </div>
            <div class="flex flex-wrap items-center gap-2 text-s">
              <Button tone="primary" onclick={copyRecoveryCodes}>
                {recoveryCopied ? t("account_2fa_recovery_copied") : t("account_2fa_recovery_copy")}
              </Button>
              <Button onclick={downloadRecoveryCodes}>
                {t("account_2fa_recovery_download")}
              </Button>
              <Button onclick={() => (recoveryCodes = null)}>
                {t("account_2fa_recovery_confirm")}
              </Button>
            </div>
            <p class="text-s text-amber-400">⚠ {t("account_2fa_recovery_warning")}</p>
          </div>
        {/if}
      </div>
  </Card>

  <Card title={t("account_section_preferences")}>
      <form onsubmit={savePreferences} class="space-y-4">
        <div class="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <Field label={t("pref_default_landing")} id="pref-landing">
            <select id="pref-landing" bind:value={prefLanding} class="w-full">
              {#each LANDING_OPTIONS as opt (opt.value)}
                <option value={opt.value}>{landingLabel(opt)}</option>
              {/each}
            </select>
          </Field>

          <Field label={t("pref_time_format")}>
            <div class="inline-flex rounded border border-baba-border text-s overflow-hidden">
              {#each TIME_FORMATS as opt, i (opt.v)}
                <label class="relative cursor-pointer px-3 py-1.5 transition-colors
                  {i > 0 ? 'border-l border-baba-border' : ''}
                  {prefTimeFormat === opt.v
                    ? 'bg-baba-accent/10 text-baba-accent'
                    : 'bg-baba-panel-2 text-baba-text-muted hover:bg-baba-panel'}">
                  <input type="radio" bind:group={prefTimeFormat} value={opt.v} class="sr-only" />
                  {t(opt.k)}
                </label>
              {/each}
            </div>
          </Field>

          <Field label={t("pref_timezone")} id="pref-tz">
            <input id="pref-tz" type="text" list="tz-list"
              bind:value={prefTimezone}
              placeholder={t("pref_timezone_auto")}
              autocomplete="off" spellcheck="false"
              class="w-full font-mono" />
            <datalist id="tz-list">
              {#each TZ_COMMON as tz (tz)}<option value={tz}></option>{/each}
            </datalist>
          </Field>

          <Field label={t("pref_theme")}>
            <div class="inline-flex rounded border border-baba-border text-s overflow-hidden">
              {#each THEME_OPTIONS as opt, i (opt.v)}
                <label class="relative cursor-pointer px-3 py-1.5 transition-colors
                  {i > 0 ? 'border-l border-baba-border' : ''}
                  {prefTheme === opt.v
                    ? 'bg-baba-accent/10 text-baba-accent'
                    : 'bg-baba-panel-2 text-baba-text-muted hover:bg-baba-panel'}">
                  <input type="radio" bind:group={prefTheme} value={opt.v} class="sr-only" />
                  {t(opt.k)}
                </label>
              {/each}
            </div>
          </Field>

          <Field label={t("pref_language")}>
            <div class="inline-flex rounded border border-baba-border text-s overflow-hidden">
              {#each LOCALE_OPTIONS as opt, i (opt.v)}
                <label class="relative cursor-pointer px-3 py-1.5 transition-colors
                  {i > 0 ? 'border-l border-baba-border' : ''}
                  {prefLocale === opt.v
                    ? 'bg-baba-accent/10 text-baba-accent'
                    : 'bg-baba-panel-2 text-baba-text-muted hover:bg-baba-panel'}">
                  <input type="radio" bind:group={prefLocale} value={opt.v} class="sr-only" />
                  {t(opt.k)}
                </label>
              {/each}
            </div>
          </Field>
        </div>

        {#if prefError}<Notice tone="err">{prefError}</Notice>{/if}

        <SaveButton type="submit" dirty={prefDirty} saving={prefBusy} label={t("pref_save")} />
      </form>
      <p class="mt-3 text-s text-baba-text-faint">{t("account_preferences_note")}</p>
  </Card>
</div>
