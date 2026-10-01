/* BABA demo mode: the real BABA frontend with its network layer swapped out.
 *
 * Loaded before the app boots (see app.html) and active only when the build was
 * made with BABA_DEMO=1. Unlike DIDA (one api.ts seam), BABA scatters ~150
 * fetch/WebSocket calls across many files, so we patch the two GLOBAL seams —
 * fetch and WebSocket — which catches every one:
 *   • GET    → answered from a recorded snapshot of a real (anonymised) house
 *   • writes → accepted, applied to the in-memory snapshot, never persisted
 *   • WS     → connects and stays quiet (no live pushes to replay)
 *   • camera pixels (live.jpg / stream.mp4) → served as static blurred assets
 * Every camera frame in the demo is a HAND-REVIEWED, blurred still/clip; nothing
 * here is a live feed. A reload resets the house.
 */
(function () {
  "use strict";

  const BASE = "/api";
  let db = null; // path -> recorded body (mutated by writes during the session)

  const ready = fetch("/demo-fixtures.json")
    .then((r) => r.json())
    .then((j) => (db = j))
    .catch(() => (db = {}));

  const jsonRes = (body, status = 200) =>
    new Response(JSON.stringify(body === undefined ? null : body), {
      status,
      headers: { "content-type": "application/json" },
    });

  // Fixtures keyed by path (+query when recorded). Fall back to the bare path so
  // `?t=…`/`?since=…`-style params still resolve; then to any recording of the
  // same bare path (time-ranged endpoints never repeat their exact key).
  function lookup(path) {
    if (path in db) return db[path];
    const bare = path.split("?")[0];
    if (bare in db) return db[bare];
    for (const k of Object.keys(db)) {
      if (k.split("?")[0] === bare) return db[k];
    }
    return undefined;
  }

  // A write updates the snapshot where the shape makes that obvious, so the UI
  // reads its own change back instead of snapping to the old value.
  function applyWrite(path, method, body) {
    const bare = path.split("?")[0];
    const coll = bare.replace(/\/[^/]+$/, "");
    const idPart = bare.slice(coll.length + 1);
    if (method === "DELETE" && Array.isArray(db[coll])) {
      db[coll] = db[coll].filter((r) => String(r.id ?? r.gid ?? r.key ?? "") !== idPart);
      return { ok: true };
    }
    if (Array.isArray(db[bare]) && method === "POST") {
      const row = { id: Date.now(), ...(body || {}) };
      db[bare] = [...db[bare], row];
      return row;
    }
    if (Array.isArray(db[coll]) && (method === "PUT" || method === "PATCH")) {
      let out = body;
      db[coll] = db[coll].map((r) => {
        if (String(r.id ?? r.gid ?? r.key ?? "") !== idPart) return r;
        out = { ...r, ...(body || {}) };
        return out;
      });
      return out;
    }
    if (bare in db && body && typeof body === "object" && !Array.isArray(db[bare])) {
      db[bare] = { ...db[bare], ...body };
      return db[bare];
    }
    return body ?? { ok: true };
  }

  const realFetch = window.fetch.bind(window);

  window.fetch = async function (input, init) {
    const url = typeof input === "string" ? input : input?.url ?? String(input);
    const method = (init?.method || (typeof input !== "string" && input?.method) || "GET").toUpperCase();

    if (!url.includes(BASE + "/")) return realFetch(input, init);
    await ready;
    // Answer on a macrotask, not a microtask: a real backend has latency, and a
    // shim that resolves in zero time makes loading states flash unnaturally and
    // collapses chained fetches into a single tick. A small LAN-like delay keeps
    // the demo feeling like a live system.
    await new Promise((r) => setTimeout(r, 35));

    const path = url.slice(url.indexOf(BASE) + BASE.length) || "/";

    // Identity synthesised, never recorded: whether a recording captures the
    // session endpoint depends on cache timing, and without a user the app
    // renders an empty shell — too fragile to leave to chance.
    const DEMO_USER = {
      id: "1", username: "demo", name: "Demo", role: "admin",
      is_admin: true, theme: "dark", locale: "hr",
    };
    if (/^\/(auth|session|me|user)\b/.test(path) && method === "GET") {
      return jsonRes(lookup(path) ?? DEMO_USER);
    }
    if (/^\/(auth\/)?(login|logout)/.test(path)) return jsonRes({ ok: true, user: DEMO_USER });
    if (path.startsWith("/version")) return jsonRes(lookup("/version") ?? { version: "demo" });
    if (path.startsWith("/health")) return jsonRes({ ok: true });

    // Camera PIXELS are copied into the build as blurred static assets, so those
    // requests go to the static server. Scope to the pixel paths only:
    //   • /cameras/<id>/live.jpg   → the polled grid still (blurred)
    //   • /stream.mp4?src=<slug>   → the drill-in loop clip (blurred)
    //   • /cameras/<id>/snapshot   → poster frame
    // A blanket /cameras/ passthrough would also catch JSON calls like
    // /cameras/<id>/events, which would then receive the SPA's HTML and blow up.
    // Live solo view: /stream.mp4?src=<slug> → that camera's blurred clip.
    const sm = path.match(/^\/stream\.mp4\b.*?[?&]src=([^&]+)/);
    if (sm) return realFetch(`/demo-clip-${sm[1]}.mp4`);
    if (/^\/cameras\/[^/]+\/(live\.jpg|snapshot)/.test(path)
        || /^\/(thumbnails|crops)\//.test(path)
        || /^\/recordings\/cameras\/[^/]+\/clip/.test(path)
        || /^\/stream\.(mp4|m3u8|mjpeg)/.test(path)
        || /^\/(frame|thumbnails?)\b/.test(path)) {
      return realFetch(input, init);
    }

    if (method === "GET") {
      const hit = lookup(path);
      if (hit === undefined) {
        console.warn("[demo] no fixture for", path);
        return jsonRes([], 200);
      }
      return jsonRes(hit);
    }

    let body;
    try { body = init?.body ? JSON.parse(init.body) : undefined; } catch { body = undefined; }
    return jsonRes(applyWrite(path, method, body));
  };

  // The app opens sockets for live state / WebRTC signaling. Give them one that
  // connects and then says nothing — no reconnect storm, no fabricated events.
  class DemoSocket extends EventTarget {
    constructor() {
      super();
      this.readyState = 1;
      this.url = "demo://ws";
      setTimeout(() => this.dispatchEvent(new Event("open")), 0);
    }
    send() {}
    close() { this.readyState = 3; this.dispatchEvent(new Event("close")); }
  }
  for (const p of ["onopen", "onmessage", "onerror", "onclose"]) {
    Object.defineProperty(DemoSocket.prototype, p, {
      set(fn) { this.addEventListener(p.slice(2), fn); },
      configurable: true,
    });
  }
  DemoSocket.OPEN = 1;
  DemoSocket.CLOSED = 3;
  window.WebSocket = DemoSocket;

  // The app opens several Server-Sent-Events streams (/api/sse/detections,
  // /tracks, /events, /identities). Left real, each connects to a backend that
  // isn't there, gets the SPA's HTML, errors, and RECONNECTS every few seconds.
  // Four such retry loops saturate the browser's ~6-connections-per-host budget,
  // so a page's data fetches never get a socket and hang on "Loading…" until a
  // hard refresh. Stub it: open once, stay silent, make no real request.
  class DemoEventSource extends EventTarget {
    constructor() {
      super();
      this.readyState = 1;
      this.url = "demo://sse";
      this.withCredentials = false;
      setTimeout(() => this.dispatchEvent(new Event("open")), 0);
    }
    close() { this.readyState = 2; }
  }
  for (const p of ["onopen", "onmessage", "onerror"]) {
    Object.defineProperty(DemoEventSource.prototype, p, {
      set(fn) { this.addEventListener(p.slice(2), fn); },
      configurable: true,
    });
  }
  DemoEventSource.CONNECTING = 0;
  DemoEventSource.OPEN = 1;
  DemoEventSource.CLOSED = 2;
  window.EventSource = DemoEventSource;

  // Say plainly what this is.
  function banner() {
    const el = document.createElement("div");
    el.textContent = "DEMO · no backend — anonymised and blurred; edits are local and vanish on reload";
    Object.assign(el.style, {
      position: "fixed", left: "0", right: "0", bottom: "0", zIndex: "2147483647",
      padding: "6px 12px", textAlign: "center", pointerEvents: "none",
      font: "500 12px/1.4 system-ui, sans-serif", letterSpacing: ".01em",
      color: "#0c1417", background: "#4ea1c9", opacity: "0.94",
    });
    document.body.appendChild(el);
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", banner);
  } else {
    banner();
  }
})();
