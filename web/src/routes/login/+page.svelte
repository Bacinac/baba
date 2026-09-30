<script lang="ts">
  import { goto } from "$app/navigation";
  import { theme, Button } from "$lib/kit";
  import { t } from "$lib/i18n";
  import { auth } from "$lib/auth.svelte";
  import { onMount } from "svelte";
  import LanguageSwitcher from "$lib/LanguageSwitcher.svelte";
  import ThemeSwitcher from "$lib/ThemeSwitcher.svelte";
  import Brand from "$lib/Brand.svelte";

  onMount(() => theme.sync());

  let username = $state("");
  let password = $state("");
  let totpCode = $state("");
  // When the backend returns 2fa_required after a password-good login,
  // we switch the form into a TOTP-only stage. The username + password
  // stay in memory but the inputs are hidden so the operator only
  // sees the code field.
  let stage = $state<"password" | "totp">("password");
  let busy = $state(false);
  let error = $state<string | null>(null);

  function landAfterLogin() {
    // Preferences were already fetched + applied by auth.login (auth.prefs) —
    // no second /auth/preferences round-trip. Only accept a same-origin
    // absolute path: `startsWith("/")` alone admits protocol-relative URLs like
    // "//evil.com", so reject the "//" (and "/\") forms — a tampered preference
    // must not become an open redirect.
    let dest = "/";
    const land = auth.prefs?.default_landing;
    if (land && land.startsWith("/") && !land.startsWith("//") && !land.startsWith("/\\")) {
      dest = land;
    }
    goto(dest, { replaceState: true });
  }

  async function onSubmit(e: SubmitEvent) {
    e.preventDefault();
    busy = true;
    error = null;
    const result = await auth.login(
      username, password,
      stage === "totp" ? totpCode : undefined,
    );
    busy = false;
    if (result === null) {
      landAfterLogin();
      return;
    }
    if (result === "2fa_required") {
      // First stage succeeded; prompt for the code.
      stage = "totp";
      return;
    }
    if (result === "invalid_2fa") {
      error = t("login_error_invalid_2fa");
      return;
    }
    error = result === "invalid"
      ? t("login_error_invalid")
      : t("login_error_network");
  }

  function backToPassword() {
    stage = "password";
    totpCode = "";
    error = null;
  }
</script>

<div class="grid h-full place-items-center bg-baba-bg px-4 text-baba-text">
  <div class="w-full max-w-sm">
    <div class="mb-8 flex justify-center">
      <Brand size="md" />
    </div>

    <form
      onsubmit={onSubmit}
      class="space-y-4 rounded-lg border border-baba-border bg-baba-panel p-6"
    >
      <div>
        <h2 class="text-xl font-medium">{t("login_title")}</h2>
        <p class="text-s text-baba-text-faint">
          {stage === "password" ? t("login_subtitle") : t("login_2fa_prompt")}
        </p>
      </div>

      {#if stage === "password"}
        <div>
          <label class="block text-s text-baba-text-muted" for="username">{t("login_username")}</label>
          <input
            id="username"
            bind:value={username}
            autocomplete="username"
            required
            class="w-full rounded border border-baba-border bg-baba-panel-2 px-2 py-1.5 text-m focus:border-baba-accent focus:outline-none"
          />
        </div>

        <div>
          <label class="block text-s text-baba-text-muted" for="password">{t("login_password")}</label>
          <input
            id="password"
            type="password"
            bind:value={password}
            autocomplete="current-password"
            required
            class="w-full rounded border border-baba-border bg-baba-panel-2 px-2 py-1.5 text-m focus:border-baba-accent focus:outline-none"
          />
        </div>
      {:else}
        <div>
          <label class="block text-s text-baba-text-muted" for="totp">{t("login_2fa_code")}</label>
          <input
            id="totp"
            inputmode="numeric"
            autocomplete="one-time-code"
            bind:value={totpCode}
            required
            maxlength="8"
            pattern="[0-9]*"
            class="w-full rounded border border-baba-border bg-baba-panel-2 px-2 py-1.5 text-center font-mono text-xl tracking-widest focus:border-baba-accent focus:outline-none"
          />
        </div>
      {/if}

      {#if error}
        <p class="text-m text-red-400">{error}</p>
      {/if}

      <div class="grid"><Button tone="primary" type="submit" disabled={busy}>
        {#if busy}
          {t("login_submitting")}
        {:else if stage === "totp"}
          {t("login_2fa_verify")}
        {:else}
          {t("login_submit")}
        {/if}
      </Button></div>

      {#if stage === "totp"}
        <div class="grid"><Button size="small" onclick={backToPassword}>{t("login_2fa_back")}</Button></div>
      {/if}
    </form>

    <div class="mt-6 flex items-center justify-center gap-1">
      <ThemeSwitcher />
      <LanguageSwitcher />
    </div>
  </div>
</div>
