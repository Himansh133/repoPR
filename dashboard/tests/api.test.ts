import { describe, expect, it, vi } from "vitest";
import { ApiError, getKey, request, setKey, setUnauthorizedHandler } from "../src/api";
import { json, stubFetch } from "./helpers";

describe("api client", () => {
  it("sends the key only as the X-API-Key header", async () => {
    setKey("k-1234567890abcdef");
    const fetchMock = stubFetch(() => json(200, { runs: [] }));
    await request("/runs?limit=1");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/runs?limit=1");
    expect(new Headers(init.headers).get("X-API-Key")).toBe("k-1234567890abcdef");
    expect(url).not.toContain("k-1234567890abcdef");
    expect(localStorage.length).toBe(0);
  });

  it("refuses non same-origin paths", async () => {
    const fetchMock = stubFetch(() => json(200, {}));
    await expect(request("https://evil.example/runs")).rejects.toThrow("same-origin");
    await expect(request("//evil.example/runs")).rejects.toThrow("same-origin");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("clears the key and notifies on 401", async () => {
    setKey("k-1234567890abcdef");
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    stubFetch(() => json(401, { detail: "Missing or wrong X-API-Key" }));
    await expect(request("/runs")).rejects.toMatchObject({ status: 401 });
    expect(getKey()).toBeNull();
    expect(handler).toHaveBeenCalledOnce();
    setUnauthorizedHandler(null);
  });

  it("uses the server's detail as the error message", async () => {
    stubFetch(() => json(409, { detail: "A run is already in progress" }));
    const err = await request("/runs").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).message).toBe("A run is already in progress");
  });

  it("falls back to a generic message for non-JSON errors", async () => {
    stubFetch(() => new Response("oops", { status: 500 }));
    await expect(request("/runs")).rejects.toThrow("Request failed (500)");
  });
});
