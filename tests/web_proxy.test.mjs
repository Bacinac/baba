import assert from "node:assert/strict";
import http from "node:http";
import { once } from "node:events";
import test from "node:test";

async function listen(handler) {
  const server = http.createServer(handler);
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  return server;
}

function request(server, path, cookie = "viewer", method = "GET") {
  return new Promise((resolve, reject) => {
    const req = http.request({ hostname: "127.0.0.1", port: server.address().port,
      path, method, headers: { cookie, "x-forwarded-for": "192.0.2.1" } }, (res) => {
      let body = "";
      res.on("data", chunk => body += chunk);
      res.on("end", () => resolve({ status: res.statusCode, body, headers: res.headers }));
    });
    req.on("error", reject);
    req.end();
  });
}

test("shared dev and production proxy enforces media authorization before upstream access", async t => {
  const received = [];
  const api = await listen((req, res) => {
    res.setHeader("content-type", "application/json");
    if (req.url === "/auth/me") return res.end(JSON.stringify({ role: req.headers.cookie }));
    if (req.url === "/cameras/stream-access/yard" || req.url === "/cameras/stream-access/yard_sub") {
      return res.end(JSON.stringify({ stream: req.url.split("/").at(-1) }));
    }
    res.writeHead(404).end("{}");
  });
  const rtc = await listen((req, res) => {
    received.push({ path: req.url, headers: req.headers });
    res.end("media");
  });
  process.env.BABA_API_URL = `http://127.0.0.1:${api.address().port}`;
  process.env.BABA_GO2RTC_URL = `http://127.0.0.1:${rtc.address().port}`;
  process.env.BABA_GO2RTC_API_PASSWORD = "test-password";
  const { handleProxy, mediaStream, proxyTarget } = await import("../web/proxy.mjs");
  const proxy = await listen((req, res) => {
    if (!handleProxy(req, res)) res.writeHead(404).end();
  });
  t.after(() => {
    for (const server of [proxy, rtc, api]) {
      server.closeAllConnections();
      server.close();
    }
  });

  for (const path of [
    "/go2rtc/api/stream.mp4/../../config?src=yard",
    "/go2rtc/api/stream.mp4/%2e%2e/%2e%2e/config?src=yard",
    "/go2rtc/api/stream.mp4/%252e%252e/config?src=yard",
    "/go2rtc//evil.invalid/api/config", "/go2rtc/%5capi/config", "/api/%2e%2e/config",
  ]) {
    const result = await request(proxy, path);
    assert.equal(result.status, 400, path);
  }
  for (const path of [
    "/go2rtc/api/config", "/go2rtc/api/streams", "/go2rtc/api/ws?src=yard",
    "/go2rtc/api/stream.mp4?src=http%3A%2F%2F127.0.0.1%2Fprivate&name=owned",
    "/go2rtc/api/stream.mp4?src=yard&name=owned",
    "/go2rtc/api/stream.mp4?src=yard&src=yard_sub",
    "/go2rtc/api/stream.mp4?src=unregistered",
    "/go2rtc/api/stream.mp4?src=ffmpeg%3Ayard",
    "/go2rtc/api/stream.mp4?width=1280",
    "/go2rtc/api/stream.mp4?src=yard&width=1280",
    "/go2rtc/api/frame.jpeg?src=yard&width=wide",
    "/go2rtc/api/frame.jpeg?src=yard&width=1280&name=owned",
  ]) {
    assert.equal((await request(proxy, path)).status, 403, path);
  }
  assert.equal((await request(proxy, "/go2rtc/api/stream.mp4?src=yard", "viewer", "POST")).status, 403);
  assert.equal((await request(proxy, "/go2rtc/api/frame.jpeg?src=yard", "")).status, 401);
  assert.equal((await request(proxy, "/go2rtc/api/stream.mp4", "")).status, 401);
  assert.equal((await request(proxy, "/go2rtc/api/stream.mp4", "viewer", "POST")).status, 403);
  assert.equal(received.length, 0);

  for (const [path, cookie] of [
    ["/go2rtc/api/stream.mp4?src=yard", "viewer"],
    ["/go2rtc/api/frame.jpeg?src=yard_sub", "operator"],
    ["/go2rtc/api/frame.jpeg?src=yard&width=1280", "viewer"],
    ["/go2rtc/api/stream.mp4", "viewer"],
    ["/go2rtc/api/config", "admin"],
  ]) {
    const response = await request(proxy, path, cookie);
    assert.equal(response.status, 200);
    assert.equal(response.headers["cache-control"], "no-store");
  }
  for (const { headers } of received) {
    assert.equal(headers.cookie, undefined);
    assert.equal(headers.authorization, "Basic " + Buffer.from("baba:test-password").toString("base64"));
    assert.equal(headers["x-forwarded-for"], "127.0.0.1");
  }
  assert.equal(proxyTarget("/go2rtc/api/config?x=1", process.env.BABA_GO2RTC_URL, "/go2rtc").hostname, "127.0.0.1");
  assert.equal(mediaStream(new URL("http://x/api/stream.mp4?src=yard"), "GET"), "yard");
  assert.equal(mediaStream(new URL("http://x/api/stream.mp4?src=yard&video=h264"), "GET"), null);
  assert.equal(mediaStream(new URL("http://x/api/frame.jpeg?src=yard&width=1280&height=720"), "GET"), "yard");
});
