<script lang="ts">
  import { onMount } from "svelte";
  import { api, type BabaUser } from "$lib/api";
  import { ApiError, PageHead } from "$lib/kit";
  import { auth } from "$lib/auth.svelte";
  import { Button, Card, Field, Notice, dialog, toasts, Tag } from "$lib/kit";
  import { t, type MessageKey } from "$lib/i18n";
  import { dt } from "$lib/datetime.svelte";

  let users = $state<BabaUser[]>([]);
  let loading = $state(true);
  let error = $state<string | null>(null);
  let adminOnly = $state(false);

  // Add-user inline form
  let showAdd = $state(false);
  let newUsername = $state("");
  let newPassword = $state("");
  let newPasswordConfirm = $state("");
  let newRole = $state<"admin" | "operator" | "viewer">("operator");
  let addBusy = $state(false);
  let addError = $state<string | null>(null);

  // Per-row expanded reset form
  let expandedId = $state<string | null>(null);
  let resetPassword = $state("");
  let resetPasswordConfirm = $state("");
  let resetBusy = $state(false);
  let resetError = $state<{ id: string; text: string } | null>(null);
  let savingRole = $state<string | null>(null);

  const ROLES: { value: "admin" | "operator" | "viewer"; key: MessageKey }[] = [
    { value: "admin",    key: "users_role_admin" },
    { value: "operator", key: "users_role_operator" },
    { value: "viewer",   key: "users_role_viewer" },
  ];

  async function refresh() {
    loading = true; error = null;
    try { users = await api.listUsers(); }
    catch (e) {
      adminOnly = e instanceof ApiError && e.status === 403;
      error = (e as Error).message;
    }
    finally { loading = false; }
  }

  async function addUser(e: SubmitEvent) {
    e.preventDefault();
    addError = null;
    if (newPassword !== newPasswordConfirm) { addError = t("users_password_mismatch"); return; }
    addBusy = true;
    try {
      await api.createUser({ username: newUsername, password: newPassword, role: newRole });
      newUsername = ""; newPassword = ""; newPasswordConfirm = ""; newRole = "operator";
      showAdd = false;
      await refresh();
    } catch (err) { addError = (err as Error).message; }
    finally { addBusy = false; }
  }

  async function changeRole(u: BabaUser, role: "admin" | "operator" | "viewer") {
    if (u.role === role) return;
    savingRole = u.id;
    try { await api.patchUser(u.id, { role }); await refresh(); }
    catch (err) {
      // Re-read before saying anything: the `<select>` binds to `u.role`, so a
      // refused change left the dropdown displaying the role the server had
      // just declined to store — the operator closes the dialog and the screen
      // tells them it worked.
      await refresh();
      await dialog.alert({
        title: t("dialog_error_title"),
        message: (err as Error).message,
      });
    }
    finally { savingRole = null; }
  }

  async function doReset(u: BabaUser) {
    if (resetPassword.length < 8) {
      resetError = { id: u.id, text: t("users_password_min") }; return;
    }
    if (resetPassword !== resetPasswordConfirm) {
      resetError = { id: u.id, text: t("users_password_mismatch") }; return;
    }
    resetBusy = true; resetError = null;
    try {
      await api.resetUserPassword(u.id, resetPassword);
      toasts.success(t("users_password_reset_done"));
      resetPassword = ""; resetPasswordConfirm = ""; expandedId = null;
    } catch (err) { resetError = { id: u.id, text: (err as Error).message }; }
    finally { resetBusy = false; }
  }

  async function reset2faAdmin(u: BabaUser) {
    const ok = await dialog.confirm({
      title: `${t("users_reset_2fa_confirm")} "${u.username}"?`,
      message: t("users_reset_2fa_warning"),
      confirmLabel: t("users_reset_2fa"),
      danger: true,
    });
    if (!ok) return;
    try {
      await api.adminDisable2fa(u.id);
      await refresh();
      await dialog.alert({
        title: t("users_reset_2fa_done"),
        message: u.username,
      });
    } catch (err) {
      await dialog.alert({
        title: t("users_reset_2fa_error"),
        message: (err as Error).message,
      });
    }
  }

  async function removeUser(u: BabaUser) {
    const ok = await dialog.confirm({
      title: t("dialog_confirm_title"),
      message: `${t("users_confirm_delete")} "${u.username}"?`,
      confirmLabel: t("dialog_delete"),
      danger: true,
    });
    if (!ok) return;
    try { await api.deleteUser(u.id); await refresh(); }
    catch (err) {
      await dialog.alert({
        title: t("dialog_error_title"),
        message: (err as Error).message,
      });
    }
  }


  onMount(refresh);
</script>

<div class="flex w-full max-w-4xl flex-col gap-6">
  <PageHead sticky={false}>
    {#snippet aside()}
      <Button tone="primary" onclick={() => (showAdd = !showAdd)}>
        {showAdd ? t("users_cancel") : t("users_add")}
      </Button>
    {/snippet}
  </PageHead>

  {#if showAdd}
    <Card>
      <form onsubmit={addUser} class="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Field label={t("users_username")} id="new-username">
          <input id="new-username" bind:value={newUsername}
            required pattern="[a-zA-Z0-9_.-]+"
            autocomplete="off" spellcheck="false"
            class="w-full font-mono" />
        </Field>
        <Field label={t("users_password")} id="new-pw">
          <input id="new-pw" type="password" bind:value={newPassword}
            required minlength="8" autocomplete="new-password" class="w-full" />
        </Field>
        <Field label={t("users_password_confirm")} id="new-pw2">
          <input id="new-pw2" type="password" bind:value={newPasswordConfirm}
            required minlength="8" autocomplete="new-password" class="w-full" />
        </Field>
        <Field label={t("users_role")} id="new-role">
          <select id="new-role" bind:value={newRole} class="w-full">
            {#each ROLES as r (r.value)}<option value={r.value}>{t(r.key)}</option>{/each}
          </select>
        </Field>
        {#if addError}
          <div class="sm:col-span-2 lg:col-span-4">
            <Notice tone="err">{addError}</Notice>
          </div>
        {/if}
        <div class="sm:col-span-2 lg:col-span-4">
          <Button tone="primary" type="submit" disabled={addBusy}>
            {addBusy ? t("users_saving") : t("users_add")}
          </Button>
        </div>
      </form>
    </Card>
  {/if}

  {#if loading}
    <p class="muted text-m">{t("users_loading")}</p>
  {:else if error}
    {#if adminOnly}
      <p class="muted text-m">{t("users_admin_only")}</p>
    {:else}
      <Notice tone="err">{error}</Notice>
    {/if}
  {:else if users.length === 0}
    <p class="muted text-m">{t("users_empty")}</p>
  {:else}
    <ul class="overflow-hidden rounded-lg border border-baba-border">
      {#each users as u (u.id)}
        {@const isSelf = u.id === auth.user?.id}
        <li class="border-b border-baba-border last:border-b-0 bg-baba-panel">
          <div class="flex flex-wrap items-center gap-3 px-4 py-3 text-m">
            <div class="min-w-[10rem] flex-1">
              <div class="font-medium">
                {u.username}
                {#if isSelf}<span class="ml-1 text-s text-baba-text-faint">({t("users_you")})</span>{/if}
              </div>
              <div class="text-s text-baba-text-faint">
                {t("users_created")}: {dt.short(u.created_at)} ·
                {t("users_last_login")}: {u.last_login_at ? dt.short(u.last_login_at) : t("users_never")}
              </div>
            </div>

            <select
              value={u.role}
              disabled={savingRole === u.id}
              onchange={(e) => changeRole(u, (e.currentTarget as HTMLSelectElement).value as BabaUser["role"])}
              class="rounded border border-baba-border bg-baba-panel-2 px-2 py-1 text-s focus:border-baba-accent focus:outline-none disabled:opacity-50"
            >
              {#each ROLES as r (r.value)}<option value={r.value}>{t(r.key)}</option>{/each}
            </select>

            <Button size="small"
              onclick={() => {
                expandedId = expandedId === u.id ? null : u.id;
                resetPassword = ""; resetPasswordConfirm = ""; resetError = null;
              }}
            >{t("users_reset_password")}</Button>

            {#if u.totp_enabled}
              <Tag tone="ok" title={t("users_2fa_on")}>2FA</Tag>
              <Button size="small"
                disabled={isSelf}
                onclick={() => reset2faAdmin(u)}
              >{t("users_reset_2fa")}</Button>
            {/if}

            <Button tone="danger" size="small"
              disabled={isSelf}
              onclick={() => removeUser(u)}
            >{t("users_delete")}</Button>
          </div>

          {#if expandedId === u.id}
            <div class="border-t border-baba-border bg-baba-panel-2 px-4 py-3">
              <div class="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-[1fr_1fr_auto]">
                <Field label={t("users_password_new")}>
                  <input type="password" bind:value={resetPassword}
                    minlength="8" autocomplete="new-password" class="w-full" />
                </Field>
                <Field label={t("users_password_confirm")}>
                  <input type="password" bind:value={resetPasswordConfirm}
                    minlength="8" autocomplete="new-password" class="w-full" />
                </Field>
                <div class="flex items-end">
                  <Button tone="primary" onclick={() => doReset(u)} disabled={resetBusy}>
                    {resetBusy ? t("users_saving") : t("users_save")}
                  </Button>
                </div>
              </div>
            </div>
          {/if}

          {#if resetError && resetError.id === u.id}
            <div class="border-t border-baba-border bg-baba-panel-2 px-4 py-2">
              <Notice tone="err">{resetError.text}</Notice>
            </div>
          {/if}
        </li>
      {/each}
    </ul>
  {/if}
</div>
