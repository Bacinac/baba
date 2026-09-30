import adapterNode from "@sveltejs/adapter-node";
import adapterStatic from "@sveltejs/adapter-static";
import { vitePreprocess } from "@sveltejs/vite-plugin-svelte";

// BABA_DEMO=1 builds the public, backend-less demo: a static SPA (adapter-static
// with a 200.html fallback) whose network layer is swapped by static/demo-net.js.
// Production keeps adapter-node untouched.
const demo = process.env.BABA_DEMO === "1";

/** @type {import('@sveltejs/kit').Config} */
export default {
  preprocess: vitePreprocess(),
  kit: {
    adapter: demo
      ? adapterStatic({ fallback: "index.html", strict: false })
      : adapterNode(),
  },
};
