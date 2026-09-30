<script lang="ts">
  import "../app.css";
  import { onMount } from "svelte";
  import { goto, beforeNavigate } from "$app/navigation";
  import { page } from "$app/state";
  import { Dialogs, Frame, NewVersion, Toasts, i18n, theme, type Section } from "$lib/kit";
  import { t, type MessageKey } from "$lib/i18n";
  import { auth } from "$lib/auth.svelte";
  import TopBar from "$lib/TopBar.svelte";
  import Brand from "$lib/Brand.svelte";
  import { version } from "$lib/version.svelte";
  import { help } from "$lib/help";

  let { children } = $props();

  onMount(async () => {
    // A failed dynamic import proves the bundle is stale, whatever /version says.
    window.addEventListener("vite:preloadError", (e) => {
      e.preventDefault();
      window.location.reload();
    });
    i18n.init();
    theme.sync();
    version.watch();
    await auth.fetchMe();
  });

  // When a web deploy happens under a live tab, the running bundle's lazy
  // chunks vanish (new hashed assets) — a client-side navigation would break
  // on a 404 import (the old Vite-dev era's eternal spinner, same class under
  // the prod build). Detected via /version polling: do a FULL browser
  // navigation to the target so the stale tab loads the fresh bundle
  // seamlessly, instead of a broken SPA transition.
  beforeNavigate(({ to, cancel }) => {
    if (version.available && to?.url) {
      cancel();
      window.location.href = to.url.href;
    }
  });

  $effect(() => {
    const path = page.url.pathname;
    if (auth.user === null && !path.startsWith("/login")) {
      goto("/login", { replaceState: true });
    }
  });

  type NavItem = { href: string; key: MessageKey };
  const monitoring: NavItem[] = [
    { href: "/",           key: "nav_dashboard" },
    { href: "/live",       key: "nav_live" },
    { href: "/activity",   key: "nav_sightings" },
    { href: "/search",     key: "nav_search" },
  ];
  const archive: NavItem[] = [
    { href: "/identities", key: "nav_identities" },
    { href: "/analytics",  key: "nav_analytics" },
  ];
  const settingsSubnav: NavItem[] = [
    { href: "/settings/cameras",       key: "settings_cameras" },
    { href: "/settings/detection",     key: "settings_detection" },
    { href: "/settings/ai",            key: "settings_ai" },
    { href: "/settings/face-recognition", key: "settings_face_recognition" },
    { href: "/settings/notifications", key: "settings_notifications" },
    { href: "/settings/storage",       key: "settings_storage" },
    { href: "/settings/users",         key: "settings_users" },
    { href: "/settings/audit",         key: "settings_audit" },
    { href: "/settings/system",        key: "settings_system" },
    { href: "/settings/preferences",   key: "settings_preferences" },
  ];
  // The phone's bottom bar: the three pages watched most, each with its mark.
  const icons: Record<string, string> = {
    "/": '<path d="M3 11 12 4l9 7"/><path d="M5 10v10h14V10"/>',
    "/live": '<rect x="3" y="6" width="14" height="12" rx="2"/><path d="m17 10 4-2v8l-4-2"/>',
    "/activity": '<path d="M3 12h4l2-7 4 14 2-7h6"/>',
  };
  const tabs = ["/", "/live", "/activity"];

  const toSection = (i: NavItem): Section => ({ href: i.href, label: t(i.key), icon: icons[i.href] });
  const sections = $derived<Section[][]>([
    monitoring.map(toSection),
    archive.map(toSection),
    [{ href: "/settings", label: t("nav_settings"), children: settingsSubnav.map(toSection) }],
  ]);

  let pathname = $derived(page.url.pathname);
  let isLoginRoute = $derived(pathname.startsWith("/login"));

  async function logout() {
    await auth.logout();
    goto("/login", { replaceState: true });
  }
</script>

{#if isLoginRoute}
  {@render children()}
{:else if auth.user === undefined}
  <div class="grid h-full place-items-center bg-baba-bg text-m text-baba-text-faint">
    {t("auth_loading")}
  </div>
{:else if auth.user === null}
  <div class="h-full bg-baba-bg"></div>
{:else}
  <Frame
    {pathname}
    {sections}
    {tabs}
    account={{
      name: auth.user.username,
      role: t(`users_role_${auth.user.role}` as MessageKey),
      href: "/account",
      onlogout: logout,
    }}
    {help}
    version={version.label}
  >
    {#snippet brand()}<Brand />{/snippet}
    {#snippet bar()}<TopBar />{/snippet}
    {@render children()}
  </Frame>
{/if}

<!-- Mounted at app root so any page can open a unified confirm/alert
     without needing to render its own dialog tree. -->
<Dialogs />
<Toasts />
<NewVersion watch={version} />
