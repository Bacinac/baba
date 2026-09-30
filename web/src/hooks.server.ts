import type { Handle } from "@sveltejs/kit";

// The SSR document must never be cached: it names the hashed chunks of one
// build, and a heuristically-cached copy keeps a long-lived window on a stale
// bundle across deploys while /version (fetched live) claims it is current.
// Hashed assets under /_app/immutable are cache-busted by name and unaffected.
export const handle: Handle = async ({ event, resolve }) => {
  const response = await resolve(event);
  if (response.headers.get("x-sveltekit-page") === "true") {
    response.headers.set("cache-control", "no-store");
  }
  return response;
};
