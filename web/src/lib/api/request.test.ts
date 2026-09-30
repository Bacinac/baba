import { afterEach, describe, expect, it, vi } from "vitest";
import { t } from "$lib/i18n";
import { ApiError } from "$lib/kit";
import { api } from "./index";

function reply(status: number, body?: unknown): Response {
  return new Response(body === undefined ? null : JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

afterEach(() => vi.unstubAllGlobals());

describe("request", () => {
  it("goes to the api under /api with the session cookie", async () => {
    const fetch = vi.fn(() => Promise.resolve(reply(200, [])));
    vi.stubGlobal("fetch", fetch);
    await api.listCameras();
    expect(fetch).toHaveBeenCalledWith("/api/cameras", expect.objectContaining({ credentials: "include" }));
  });

  it("says a server that did not answer in the kit's words", async () => {
    vi.stubGlobal("fetch", () => Promise.reject(new TypeError("Failed to fetch")));
    await expect(api.listCameras()).rejects.toThrow(t("common.unreachable"));
  });
});

describe("a refused request", () => {
  it("carries the api's own sentence and status", async () => {
    vi.stubGlobal("fetch", () => Promise.resolve(reply(409, { detail: "slug already taken" })));
    const e = await api.listCameras().catch((e: unknown) => e);
    expect(e).toBeInstanceOf(ApiError);
    expect((e as ApiError).status).toBe(409);
    expect((e as ApiError).message).toBe("slug already taken");
  });

  it("names a missing session and a missing right in the reader's words", async () => {
    vi.stubGlobal("fetch", () => Promise.resolve(reply(401, { detail: "Not authenticated" })));
    await expect(api.listCameras()).rejects.toThrow(t("api_unauthorized"));
    vi.stubGlobal("fetch", () => Promise.resolve(reply(403, { detail: "admin only" })));
    await expect(api.listCameras()).rejects.toThrow(t("api_forbidden"));
  });

  it("falls back to the status when the body says nothing", async () => {
    vi.stubGlobal("fetch", () => Promise.resolve(new Response("upstream down", { status: 502 })));
    await expect(api.listCameras()).rejects.toThrow(t("common.failed", { status: 502 }));
  });
});
