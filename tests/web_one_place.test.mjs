// Numbers, dates and api calls are each shaped in one place: a hand-rolled
// `toFixed` shows 2.5 to a Croatian reader, every inline `toLocaleString` picked
// its own locale, order and clock, and a fetch beside the api client shows
// "500 Internal Server Error: {"detail": …}" where the client says what failed.

import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { test } from "node:test";

const SRC = new URL("../web/src/", import.meta.url).pathname;

function sources(dir = SRC, out = []) {
  for (const e of readdirSync(dir)) {
    const p = join(dir, e);
    if (statSync(p).isDirectory()) sources(p, out);
    else if ((p.endsWith(".svelte") || p.endsWith(".ts")) && !p.endsWith(".test.ts")) out.push(p);
  }
  return out;
}

function offenders(pattern, home) {
  return sources()
    .map((p) => relative(SRC, p))
    .filter((f) => !home.has(f))
    .flatMap((f) =>
      readFileSync(join(SRC, f), "utf8")
        .split("\n")
        .flatMap((line, i) => (pattern.test(line) ? [`${f}:${i + 1}`] : [])),
    );
}

test("nothing formats a number or a date by hand", () => {
  const raw = /\.toFixed\(|\.toLocale(?:Date|Time)?String\(|Intl\.(?:DateTimeFormat|NumberFormat)\(/;
  assert.deepEqual(offenders(raw, new Set(["lib/kit/i18n.svelte.ts", "lib/datetime.svelte.ts"])), []);
});

test("every api call goes through the api client", () => {
  // mse.ts holds a live media stream open for as long as the tile shows it;
  // a request deadline would cut it.
  assert.deepEqual(offenders(/\bfetch\(/, new Set(["lib/api/index.ts", "lib/kit/http.ts", "lib/mse.ts"])), []);
});

test("every live stream goes through the live feed", () => {
  // A bare EventSource shut by the tunnel's 502 during a deploy never opens
  // again, and nothing fetches what it missed while it was down.
  assert.deepEqual(offenders(/new EventSource\(/, new Set(["lib/api/live.svelte.ts"])), []);
});

// A button, a panel, a window and a tag are each drawn by the kit. The screens
// had 176 buttons, 48 panels, four windows and fifty tags painted by hand, each
// a shade off the next, and none of the windows kept the keyboard inside.
function openingTags(text, name) {
  const out = [];
  const start = new RegExp(`<${name}\\b`, "g");
  for (let m; (m = start.exec(text)); ) {
    let i = m.index + name.length + 1;
    let quote = null;
    let depth = 0;
    for (; i < text.length; i++) {
      const c = text[i];
      if (quote) {
        if (c === quote) quote = null;
      } else if (c === '"' || c === "'") quote = c;
      else if (c === "{") depth++;
      else if (c === "}") depth--;
      else if (c === ">" && depth === 0) break;
    }
    out.push({ at: m.index, attrs: text.slice(m.index + name.length + 1, i) });
  }
  return out;
}

function handDrawn(names, looksLike) {
  return sources()
    .map((p) => relative(SRC, p))
    .filter((f) => f.endsWith(".svelte") && !f.startsWith("lib/kit/"))
    .flatMap((f) => {
      const text = readFileSync(join(SRC, f), "utf8");
      return names.flatMap((name) =>
        openingTags(text, name)
          .filter(({ attrs }) => looksLike(attrs, (/\bclass="([^"]*)"/.exec(attrs)?.[1] ?? "").split(/\s+/)))
          .map(({ at }) => `${f}:${text.slice(0, at).split("\n").length}`),
      );
    });
}

const has = (words, re) => words.some((w) => re.test(w));

test("a button is the kit's Button", () => {
  const dressed = (_, w) => has(w, /^rounded/) && has(w, /^px-/) && has(w, /^py-/) && has(w, /^(border|bg-)/);
  assert.deepEqual(handDrawn(["button"], dressed), []);
});

test("a panel is the kit's Card", () => {
  const panel = (_, w) =>
    has(w, /^rounded/) && w.includes("border") && w.includes("border-baba-border") && w.includes("bg-baba-panel") && has(w, /^p-[3-9]$/);
  assert.deepEqual(handDrawn(["div", "section"], panel), []);
});

test("a window is the kit's Dialog", () => {
  assert.deepEqual(handDrawn(["div", "section"], (attrs) => /\brole="dialog"/.test(attrs)), []);
});

test("a tag is the kit's Tag", () => {
  const pill = (_, w) => has(w, /^rounded/) && has(w, /^px-/) && has(w, /^py-(0\.5|0|px)$/);
  assert.deepEqual(handDrawn(["span"], pill), []);
});

test("a list is put in order in the reader's alphabet", () => {
  // localeCompare files "Čelik" after "Zagreb" in a browser set to English
  // and knows nothing of dž, lj and nj; byLabel sorts by the kit's alphabet.
  assert.deepEqual(offenders(/\.localeCompare\(/, new Set(["lib/order.ts"])), []);
});
