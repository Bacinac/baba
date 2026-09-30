// The public demo is a static, backend-less SPA (adapter-static): disable SSR so
// no page tries to render/fetch on a server that isn't there. Production
// (VITE_BABA_DEMO unset) keeps SSR exactly as before.
export const ssr = !import.meta.env.VITE_BABA_DEMO;
export const prerender = false;
