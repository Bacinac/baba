import { sveltekit } from "@sveltejs/kit/vite";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";

// go2rtc's REST API requires basic auth (see docker-compose go2rtc service).
// The browser has no go2rtc credentials, so this proxy injects them server-side
// for the live stream. The injection is scoped to the stream-consumer endpoints
// under a safe method, exactly as the production proxy (web/server.mjs) gates a
// non-admin: the dev proxy has no session to authorise, so it must never hand
// go2rtc's admin API — /api/config, /api/streams writes, /api/restart — the one
// credential the browser is deliberately never given. Anything outside the
// allow-list is forwarded without the header, so go2rtc rejects it with 401.
const go2rtcUser = process.env.BABA_GO2RTC_API_USER || "baba";
const go2rtcPass = process.env.BABA_GO2RTC_API_PASSWORD || "";
const go2rtcAuthHeader = "Basic " + Buffer.from(`${go2rtcUser}:${go2rtcPass}`).toString("base64");
const GO2RTC_MEDIA_GET = /^\/go2rtc\/api\/(stream\.|frame\.|hls\/)/;
const go2rtcMediaRequest = (req: { method?: string; url?: string }) =>
  (req.method === "GET" || req.method === "HEAD") &&
  GO2RTC_MEDIA_GET.test((req.url || "").split("?")[0]);

const publicHosts = (process.env.BABA_CORS_ORIGINS ?? "")
  .split(",")
  .map((origin) => origin.trim().replace(/^https?:\/\//, "").replace(/:\d+$/, ""))
  .filter(Boolean);

export default defineConfig({
  plugins: [tailwindcss(), sveltekit()],
  server: {
    host: "0.0.0.0",
    port: 5173,
    strictPort: true,
    allowedHosts: [...publicHosts, "localhost", "127.0.0.1"],
    // BABA_API_URL is read at runtime via PUBLIC_API_URL; dev proxy avoids
    // CORS by routing /api/* to the FastAPI service.
    proxy: {
      "/api": {
        target: process.env.BABA_API_URL || "http://api:8080",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
        ws: true,
      },
      "/go2rtc": {
        target: process.env.BABA_GO2RTC_URL || "http://host.docker.internal:1984",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/go2rtc/, ""),
        ws: true,
        configure: (proxy) => {
          if (!go2rtcPass) {
            throw new Error("BABA_GO2RTC_API_PASSWORD is required — run ./install.sh --upgrade");
          }
          proxy.on("proxyReq", (proxyReq, req) => {
            if (go2rtcMediaRequest(req)) {
              proxyReq.setHeader("Authorization", go2rtcAuthHeader);
            }
          });
          // No credential injection on WebSocket upgrades: the UI has no go2rtc
          // WS consumers, and the admin API is reachable over WS too.
        },
      },
    },
  },
});
