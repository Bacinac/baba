// Every date and time the UI shows goes through `dt`: one shape per kind of
// moment, in the active language and the user's timezone and 12h/24h
// preference. `toLocale*String` or `Intl.DateTimeFormat` anywhere else is a
// test failure (tests/web_one_place.test.mjs).
//
// The store is hydrated from /auth/preferences after login (see auth flow).
// Before hydration it falls back to browser defaults, which is what every
// page would do anyway, so nothing breaks.

import { i18n } from "$lib/kit";
import type { UserPreferences } from "$lib/api";

type When = Date | number | string | null | undefined;

class DateTimeStore {
  prefs = $state<UserPreferences>({});

  setPrefs(p: UserPreferences | null) {
    this.prefs = p ?? {};
  }

  private get _locale(): string {
    return i18n.locale === "en" ? "en-GB" : "hr-HR";
  }

  /** Build Intl options that respect the user's prefs. */
  private _opts(partial: Intl.DateTimeFormatOptions): Intl.DateTimeFormatOptions {
    const opts: Intl.DateTimeFormatOptions = { ...partial };
    if (this.prefs.timezone) opts.timeZone = this.prefs.timezone;
    if (this.prefs.time_format_24h !== undefined) {
      opts.hour12 = !this.prefs.time_format_24h;
    }
    return opts;
  }

  private _format(when: When, partial: Intl.DateTimeFormatOptions, none: string): string {
    if (when === null || when === undefined || when === "") return none;
    const d = when instanceof Date ? when : new Date(when);
    if (isNaN(d.getTime())) return none;
    return new Intl.DateTimeFormat(this._locale, this._opts(partial)).format(d);
  }

  /** "14 May 2026, 17:23:45" — full date + time, useful for tooltips. */
  full(when: When): string {
    return this._format(
      when,
      { year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" },
      "—",
    );
  }

  /** "14 May 17:23" — short, useful for lists. */
  short(when: When): string {
    return this._format(when, { month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" }, "—");
  }

  /** "17:23" — wall clock only, for the end of a range whose start already
   *  carries the date. */
  hm(when: When): string {
    return this._format(when, { hour: "2-digit", minute: "2-digit" }, "");
  }

  /** "17:23:45" — a moment inside a clip or a live feed. */
  hms(when: When): string {
    return this._format(when, { hour: "2-digit", minute: "2-digit", second: "2-digit" }, "");
  }

  /** "14 May" — a day heading or a day tick on a timeline. */
  day(when: When): string {
    return this._format(when, { day: "2-digit", month: "short" }, "");
  }
}

export const dt = new DateTimeStore();
