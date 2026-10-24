import adapterNode from "@sveltejs/adapter-node";
import adapterStatic from "@sveltejs/adapter-static";
import { vitePreprocess } from "@sveltejs/vite-plugin-svelte";
import { sveltekit } from "@sveltejs/kit/vite";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";
import { assertProxyConfig, handleProxy } from "./proxy.mjs";

const publicHosts = (process.env.BABA_CORS_ORIGINS ?? "")
  .split(",")
  .map((origin) => origin.trim().replace(/^https?:\/\//, "").replace(/:\d+$/, ""))
  .filter(Boolean);

// BABA_DEMO=1 builds the public, backend-less demo: a static SPA (adapter-static
// with a 200.html fallback) whose network layer is swapped by static/demo-net.js.
// Production keeps adapter-node untouched.
const demo = process.env.BABA_DEMO === "1";

export default defineConfig({
  plugins: [tailwindcss(), sveltekit({
    preprocess: vitePreprocess(),
    adapter: demo
      ? adapterStatic({ fallback: "index.html", strict: false })
      : adapterNode(),
  }), {
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
