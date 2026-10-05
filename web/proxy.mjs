import http from "node:http";
import { clientIp, trustedEdges } from "./client-ip.mjs";

const API = process.env.BABA_API_URL || "http://api:8080";
const GO2RTC = process.env.BABA_GO2RTC_URL || "http://host.docker.internal:1984";
const EDGES = trustedEdges(process.env.BABA_TRUSTED_EDGES);
const password = process.env.BABA_GO2RTC_API_PASSWORD || "";
const authorization = "Basic " + Buffer.from(`${process.env.BABA_GO2RTC_API_USER || "baba"}:${password}`).toString("base64");
const authCache = new Map();
const MEDIA_PATHS = new Set(["/api/stream.mp4", "/api/stream.mjpeg", "/api/frame.jpeg"]);

export function assertProxyConfig() {
  if (!password) throw new Error("BABA_GO2RTC_API_PASSWORD is required — run ./install.sh --upgrade");
}

export function proxyTarget(url, base, prefix) {
  const rest = url.slice(prefix.length) || "/";
  const q = rest.indexOf("?");
  let path;
  try { path = decodeURIComponent(q === -1 ? rest : rest.slice(0, q)); } catch { return null; }
  if (!path.startsWith("/") || path.startsWith("//") || /[%\\\x00-\x20]/.test(path) || /(?:^|\/)\.{1,2}(?:\/|$)/.test(path)) return null;
  const target = new URL(base);
  target.pathname = path;
  target.search = q === -1 ? "" : rest.slice(q);
  return target;
}

export function mediaStream(target, method) {
  if (!target || !["GET", "HEAD"].includes(method) || !MEDIA_PATHS.has(target.pathname)) return null;
  const params = [...target.searchParams];
  if (params.length !== 1 || params[0][0] !== "src") return null;
  return /^[a-z0-9][a-z0-9_-]{0,127}$/.test(params[0][1]) ? params[0][1] : null;
}

function fail(res, status, detail) {
  res.writeHead(status, { "content-type": "application/json", "cache-control": "no-store" });
  res.end(JSON.stringify({ detail }));
}

function probe(req, path) {
  const headers = {};
  if (req.headers.cookie) headers.cookie = req.headers.cookie;
  if (req.headers["x-peer-key"]) headers["x-peer-key"] = req.headers["x-peer-key"];
  return new Promise((resolve) => {
    const request = http.get(new URL(path, API), { headers, timeout: 5000 }, (response) => {
      let body = "";
      response.setEncoding("utf8");
      response.on("data", (chunk) => { if (body.length < 4096) body += chunk; else request.destroy(); });
      response.on("error", () => resolve(null));
      response.on("end", () => {
        if (response.statusCode !== 200) return resolve(null);
        try { resolve(JSON.parse(body)); } catch { resolve(null); }
      });
    });
    request.on("error", () => resolve(null));
    request.on("timeout", () => { request.destroy(); resolve(null); });
  });
}

async function roleOf(req) {
  const credential = req.headers["x-peer-key"] ? `k:${req.headers["x-peer-key"]}` : req.headers.cookie;
  if (!credential) return "";
  const cached = authCache.get(credential);
  if (cached?.until > Date.now()) return cached.role;
  const role = (await probe(req, "/auth/me"))?.role;
  if (!["admin", "operator", "viewer"].includes(role)) return "";
  if (authCache.size >= 512) authCache.clear();
  authCache.set(credential, { role, until: Date.now() + 30_000 });
  return role;
}

function forward(req, res, target, media = false) {
  const headers = { ...req.headers, host: target.host };
  const client = clientIp(req.socket?.remoteAddress, req.headers, EDGES);
  for (const key of ["cf-connecting-ip", "x-real-ip", "true-client-ip", "forwarded", "x-forwarded-for"]) delete headers[key];
  if (client) headers["x-forwarded-for"] = client;
  if (media) {
    headers.authorization = authorization;
    delete headers.cookie;
    delete headers["x-peer-key"];
  }
  const upstream = http.request(target, { method: req.method, headers }, (response) => {
    res.writeHead(response.statusCode ?? 502, { ...response.headers, ...(media ? { "cache-control": "no-store" } : {}) });
    response.pipe(res);
  });
  upstream.on("error", () => { if (!res.headersSent) fail(res, 502, "upstream unavailable"); else res.end(); });
  res.on("close", () => upstream.destroy());
  req.pipe(upstream);
}

async function forwardGo2rtc(req, res, target) {
  const role = await roleOf(req);
  if (!role) return fail(res, 401, "not authenticated");
  if (role !== "admin") {
    const stream = mediaStream(target, req.method);
    if (!stream) return fail(res, 403, "go2rtc request requires admin role");
    const access = await probe(req, `/cameras/stream-access/${encodeURIComponent(stream)}`);
    if (access?.stream !== stream) return fail(res, 403, "camera stream access denied");
  }
  forward(req, res, target, true);
}

export function handleProxy(req, res) {
  const url = req.url || "/";
  const api = url === "/api" || url.startsWith("/api/");
  const media = url === "/go2rtc" || url.startsWith("/go2rtc/");
  if (!api && !media) return false;
  const target = proxyTarget(url, api ? API : GO2RTC, api ? "/api" : "/go2rtc");
  if (!target) { fail(res, 400, "bad request path"); return true; }
  if (api) forward(req, res, target);
  else forwardGo2rtc(req, res, target).catch(() => {
    if (!res.headersSent) fail(res, 502, "authorization unavailable"); else res.end();
  });
  return true;
}
