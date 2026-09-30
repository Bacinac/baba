// Production web server: SvelteKit (adapter-node) + the two same-origin
// proxies the Vite dev server provided via `server.proxy`:
//
//   /api/*    → the FastAPI service        (BABA_API_URL)
//   /go2rtc/* → go2rtc on the docker host  (BABA_GO2RTC_URL), injecting
//               basic-auth server-side — the browser has no go2rtc
//               credentials (BABA_GO2RTC_API_USER/PASSWORD, both required).
//
// That injection is why /go2rtc/* MUST carry its own authorization check: the
// proxy holds credentials the caller does not, so an ungated route hands every
// anonymous request the full go2rtc API — including /api/config, which answers
// with each camera's RTSP URL, password in clear text. /api/* is safe without a
// check here only because FastAPI authenticates every route itself. The check
// delegates to the same authority (GET /auth/me), so a session cookie and the
// system-to-system X-Peer-Key both work and neither is re-implemented here.
//
// Plain node:http piping streams both directions natively, which is exactly
// what the long-lived consumers need (SSE event feeds on /api/sse/*, live
// fMP4 on /go2rtc/api/stream.mp4). No WebSocket upgrade handling on purpose:
// the UI has zero WebSocket consumers since the WS→SSE / WebRTC→MSE
// migrations — reintroduce `upgrade` proxying only if that changes.

import http from "node:http";
import { handler } from "./build/handler.js";
import { clientIp, trustedEdges } from "./client-ip.mjs";

const PORT = Number(process.env.PORT || 5173);
const API = process.env.BABA_API_URL || "http://api:8080";
const GO2RTC = process.env.BABA_GO2RTC_URL || "http://host.docker.internal:1984";
const EDGES = trustedEdges(process.env.BABA_TRUSTED_EDGES);

const go2rtcUser = process.env.BABA_GO2RTC_API_USER || "baba";
const go2rtcPass = process.env.BABA_GO2RTC_API_PASSWORD || "";
if (!go2rtcPass) {
  throw new Error("BABA_GO2RTC_API_PASSWORD is required — run ./install.sh --upgrade");
}
const go2rtcAuth = "Basic " + Buffer.from(`${go2rtcUser}:${go2rtcPass}`).toString("base64");

// Ask the API who the caller is, presenting only what the caller presented,
// and resolve to their ROLE (or "" when unauthenticated). The role decides how
// much of go2rtc the proxy will forward: injecting go2rtc's admin credentials
// for every authenticated caller handed a viewer or the DIDA peer the whole
// admin API — /api/config (each camera's RTSP password in clear text) and, via
// /api/config write + /api/restart, an unvalidated `exec:` stream, i.e. command
// execution on the host. Media consumers need only GET on the stream endpoints,
// so that is all a non-admin gets.
//
// Positive answers are cached briefly, keyed by the credential itself: a camera
// wall polling snapshots would otherwise spend one /auth/me round-trip per
// frame. The TTL is short enough that a revoked session or rotated peer key
// stops working within seconds, and only successes are cached — a rejected
// credential is re-checked every time, so nothing is retried into acceptance.
const AUTH_TTL_MS = 30_000;
const AUTH_CACHE_MAX = 512;
const authCache = new Map();

// go2rtc paths a non-admin may reach, and only with a safe method. These are
// the stream-consumer endpoints (fMP4, HLS, MJPEG, single-frame). Everything
// else — /api/config, /api/streams writes, /api/restart, /api/exit, /api/log —
// stays admin-only, because through the proxy it runs with go2rtc's admin
// credentials.
const GO2RTC_MEDIA_GET = /^\/api\/(stream\.|frame\.|hls\/)/;

function go2rtcAllowed(req, role) {
  if (role === "admin") return true;
  if (req.method !== "GET" && req.method !== "HEAD") return false;
  const rest = req.url.slice("/go2rtc".length) || "/";
  const q = rest.indexOf("?");
  const path = q === -1 ? rest : rest.slice(0, q);
  return GO2RTC_MEDIA_GET.test(path);
}

function credentialOf(req) {
  const peer = req.headers["x-peer-key"];
  if (peer) return `k:${peer}`;
  return req.headers.cookie ? `c:${req.headers.cookie}` : "";
}

// Resolves to the caller's role string, or "" if unauthenticated/unreachable.
function authorize(req) {
  const cred = credentialOf(req);
  if (!cred) return Promise.resolve("");
  const cached = authCache.get(cred);
  if (cached && cached.until > Date.now()) return Promise.resolve(cached.role);
  const headers = { host: new URL(API).host };
  if (req.headers.cookie) headers.cookie = req.headers.cookie;
  if (req.headers["x-peer-key"]) headers["x-peer-key"] = req.headers["x-peer-key"];
  return new Promise((resolve) => {
    const probe = http.request(
      new URL("/auth/me", API),
      { method: "GET", headers, timeout: 5000 },
      (ures) => {
        let body = "";
        ures.setEncoding("utf8");
        ures.on("data", (c) => {
          if (body.length < 4096) body += c;
        });
        ures.on("end", () => {
          if (ures.statusCode !== 200) return resolve("");
          let role = "";
          try {
            role = String(JSON.parse(body).role || "");
          } catch {
            role = "";
          }
          if (role) {
            if (authCache.size >= AUTH_CACHE_MAX) authCache.clear();
            authCache.set(cred, { role, until: Date.now() + AUTH_TTL_MS });
          }
          resolve(role);
        });
      },
    );
    // An unreachable API means we cannot establish who is calling — deny.
    probe.on("error", () => resolve(""));
    probe.on("timeout", () => {
      probe.destroy();
      resolve("");
    });
    probe.end();
  });
}

function proxy(req, res, targetBase, prefix, extraHeaders = {}, responseHeaders = {}) {
  // Strip the prefix and keep the rest — as a PATH, and only as a path.
  //
  // This used to be `new URL(rest, targetBase)`, and Node does not normalise
  // `req.url`: a remainder beginning with `//` is a protocol-relative URL and
  // REPLACES the base's host. `/api//192.0.2.19/` therefore fetched the
  // door camera's admin interface and handed it back through the tunnel —
  // verified 200 with 22019 bytes — and every other HTTP service this
  // container can route to with it, under any method, with the body piped and
  // the foreign response's headers written back verbatim.
  //
  // The `/api` branch carries no authorisation of its own, on the stated
  // premise that FastAPI authenticates every route itself. That premise holds
  // only while the request actually reaches FastAPI, and here it did not. The
  // `/go2rtc` branch was worse: it attaches go2rtc's basic-auth credentials
  // unconditionally, so an arbitrary host could be handed the one credential
  // the browser is deliberately never given.
  //
  // The target's host now comes from the base and nothing else.
  const rest = req.url.slice(prefix.length) || "/";
  const q = rest.indexOf("?");
  const rawPath = q === -1 ? rest : rest.slice(0, q);
  // A leading `//` or `/\` is what makes a path authority-relative. Nothing
  // this proxy legitimately serves starts that way, so refuse it loudly
  // rather than guessing at what was meant.
  if (!rawPath.startsWith("/") || /^\/[/\\]/.test(rawPath)) {
    res.writeHead(400, { "content-type": "text/plain" });
    res.end("bad request path");
    return;
  }
  const target = new URL(targetBase);
  target.pathname = rawPath;
  target.search = q === -1 ? "" : rest.slice(q);
  const headers = { ...req.headers, ...extraHeaders, host: target.host };
  // The api honours x-forwarded-for from this container, so it carries only
  // the one address this proxy vouches for; every other client-forwardable IP
  // header is dropped so nothing upstream reads a browser-forged value.
  const client = clientIp(req.socket?.remoteAddress, req.headers, EDGES);
  delete headers["cf-connecting-ip"];
  delete headers["x-real-ip"];
  delete headers["true-client-ip"];
  delete headers["forwarded"];
  if (client) headers["x-forwarded-for"] = client;
  else delete headers["x-forwarded-for"];
  const upstream = http.request(
    target,
    { method: req.method, headers },
    (ures) => {
      res.writeHead(ures.statusCode ?? 502, { ...ures.headers, ...responseHeaders });
      ures.pipe(res);
    },
  );
  upstream.on("error", () => {
    if (!res.headersSent) res.writeHead(502, { "content-type": "text/plain" });
    res.end("upstream unavailable");
  });
  // Abort the upstream request when the client goes away so SSE/stream
  // consumers don't leak upstream connections.
  res.on("close", () => upstream.destroy());
  req.pipe(upstream);
}

const server = http.createServer((req, res) => {
  const url = req.url || "/";
  // mrmime — the table SvelteKit's static handler looks types up in — has no
  // entry for .ico, so /favicon.ico goes out with an empty Content-Type and
  // Chrome quietly keeps whatever icon it already had. Set it before
  // delegating; writeHead() merges over headers already set here.
  if (url === "/favicon.ico") {
    res.setHeader("content-type", "image/x-icon");
  }
  if (url === "/api" || url.startsWith("/api/")) {
    return proxy(req, res, API, "/api");
  }
  if (url === "/go2rtc" || url.startsWith("/go2rtc/")) {
    authorize(req).then((role) => {
      if (!role) {
        res.writeHead(401, { "content-type": "application/json" });
        res.end('{"detail":"not authenticated"}');
        return;
      }
      if (!go2rtcAllowed(req, role)) {
        res.writeHead(403, { "content-type": "application/json" });
        res.end('{"detail":"go2rtc admin API requires admin role"}');
        return;
      }
      proxy(
        req,
        res,
        GO2RTC,
        "/go2rtc",
        { authorization: go2rtcAuth },
        // go2rtc sends no cache policy, and Cloudflare caches by extension:
        // stream.mp4 and frame.jpeg were replayed from the edge as a
        // recording minutes old, at CDN speed, instead of the live camera.
        { "cache-control": "no-store" },
      );
    });
    return;
  }
  handler(req, res);
});

server.listen(PORT, "0.0.0.0", () => {
  console.log(
    `baba-web (production) listening on :${PORT} — api→${API}, go2rtc→${GO2RTC}, ` +
      `trusted edges: ${process.env.BABA_TRUSTED_EDGES || "none"}`,
  );
});

// As PID 1, node ignores a signal it has no handler for: `docker stop` waited
// out its 10 s and killed the container. Live streams never end on their own,
// so they are closed, not waited for.
for (const signal of ["SIGTERM", "SIGINT"]) {
  process.once(signal, () => {
    server.close(() => process.exit(0));
    server.closeAllConnections();
  });
}
