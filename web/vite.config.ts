import { sveltekit } from "@sveltejs/kit/vite";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";
import { assertProxyConfig, handleProxy } from "./proxy.mjs";

const publicHosts = (process.env.BABA_CORS_ORIGINS ?? "")
  .split(",")
  .map((origin) => origin.trim().replace(/^https?:\/\//, "").replace(/:\d+$/, ""))
  .filter(Boolean);

export default defineConfig({
  plugins: [tailwindcss(), sveltekit(), {
    name: "baba-proxy",
    configureServer(server) {
      assertProxyConfig();
      server.middlewares.use((req, res, next) => {
        if (!handleProxy(req, res)) next();
      });
    },
  }],
  server: {
    host: "0.0.0.0",
    port: 5173,
    strictPort: true,
    allowedHosts: [...publicHosts, "localhost", "127.0.0.1"],
  },
});
