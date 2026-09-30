// Auth state: reactive `user` + login/logout. Backed by the API's session
// cookie which the browser includes automatically once set.

import { dt } from "$lib/datetime.svelte";
import { i18n, theme, type Locale, type Theme } from "$lib/kit";
import { playback } from "$lib/playback.svelte";
import { request, type UserPreferences } from "$lib/api";

export interface AuthUser {
  id: string;
  username: string;
  role: string;
}


function applyServerPrefs(prefs: UserPreferences): void {
  dt.setPrefs(prefs);
  if (prefs.theme === "light" || prefs.theme === "dark" || prefs.theme === "system") {
    theme.setTheme(prefs.theme as Theme);
  }
  if (prefs.locale === "hr" || prefs.locale === "en") {
    i18n.set(prefs.locale as Locale);
  }
  playback.setFromPrefs(prefs);
}

class AuthStore {
  /** undefined = not yet checked; null = anonymous; AuthUser = signed in. */
  user = $state<AuthUser | null | undefined>(undefined);
  checking = $state(false);
  /** Last preferences fetched during fetchMe/login. Exposed so the login
   *  page can read `default_landing` without a second /auth/preferences call. */
  prefs = $state<UserPreferences | null>(null);

  async fetchMe(): Promise<void> {
    this.checking = true;
    try {
      const r = await request("/auth/me");
      if (r.ok) {
        this.user = (await r.json()) as AuthUser;
        // Hydrate datetime + theme + locale stores from the user's server-side
        // preferences. Failure is non-fatal — UI keeps the local defaults that
        // were already applied during boot.
        try {
          const pr = await request("/auth/preferences");
          if (pr.ok) { const p = await pr.json() as UserPreferences; applyServerPrefs(p); this.prefs = p; }
        } catch { /* ignore */ }
      } else if (r.status === 401) {
        this.user = null;
        this.prefs = null;
        dt.setPrefs(null);
      } else {
        console.error("auth/me unexpected status", r.status);
        this.user = null;
      }
    } catch (err) {
      console.error("auth/me failed", err);
      this.user = null;
    } finally {
      this.checking = false;
    }
  }

  /** Returns null on success, error key on failure.
   *  "2fa_required" = first stage of password-only login succeeded
   *  and the backend is now asking for a TOTP code; UI should prompt.
   *  "invalid_2fa"  = wrong TOTP code on second attempt.
   */
  async login(
    username: string,
    password: string,
    totpCode?: string,
  ): Promise<"invalid" | "invalid_2fa" | "2fa_required" | "network" | null> {
    try {
      const body: Record<string, unknown> = { username, password };
      if (totpCode) body.totp_code = totpCode;
      const r = await request("/auth/login", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      });
      if (r.ok) {
        this.user = (await r.json()) as AuthUser;
        try {
          const pr = await request("/auth/preferences");
          if (pr.ok) { const p = await pr.json() as UserPreferences; applyServerPrefs(p); this.prefs = p; }
        } catch { /* ignore */ }
        return null;
      }
      if (r.status === 412) {
        // Backend signals "password OK, send me the TOTP code".
        return "2fa_required";
      }
      if (r.status === 401) {
        // 401 covers both wrong-password and wrong-TOTP. Disambiguate
        // via the detail field so the UI doesn't tell a 2FA user
        // their password was wrong when actually only the code was.
        try {
          const data = await r.json();
          if (data?.detail === "invalid totp_code") return "invalid_2fa";
        } catch { /* fall through */ }
        return "invalid";
      }
      return "network";
    } catch {
      return "network";
    }
  }

  async logout(): Promise<void> {
    try {
      await request("/auth/logout", { method: "POST" });
    } finally {
      this.user = null;
      this.prefs = null;
      dt.setPrefs(null);
    }
  }

  /** Change own password. Returns null on success or an error key. */
  async changePassword(old_password: string, new_password: string): Promise<"wrong" | "weak" | "network" | null> {
    if (new_password.length < 8) return "weak";
    try {
      const r = await request("/auth/change-password", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ old_password, new_password }),
      });
      if (r.status === 204) return null;
      if (r.status === 401) return "wrong";
      return "network";
    } catch {
      return "network";
    }
  }
}

export const auth = new AuthStore();
