import type { RunSummary } from "./types";

const STORAGE_KEY = "repopilot.apiKey";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

// The key lives only in sessionStorage (gone when the tab closes) and only travels as a header.
export function getKey(): string | null {
  try {
    return sessionStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

export function setKey(key: string): void {
  sessionStorage.setItem(STORAGE_KEY, key);
}

export function clearKey(): void {
  try {
    sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // storage unavailable: nothing to clear
  }
}

let onUnauthorized: (() => void) | null = null;

/** Called whenever the API answers 401, after the stored key is cleared. */
export function setUnauthorizedHandler(handler: (() => void) | null): void {
  onUnauthorized = handler;
}

export async function request<T>(path: string, init: RequestInit = {}, key = getKey()): Promise<T> {
  if (!path.startsWith("/") || path.startsWith("//")) {
    throw new Error("Only same-origin API paths are allowed");
  }
  const headers = new Headers(init.headers);
  if (key) headers.set("X-API-Key", key);
  if (init.body) headers.set("Content-Type", "application/json");
  const resp = await fetch(path, { ...init, headers });
  if (resp.status === 401) {
    clearKey();
    onUnauthorized?.();
  }
  if (!resp.ok) {
    let detail = `Request failed (${resp.status})`;
    try {
      const body: unknown = await resp.json();
      if (body && typeof body === "object" && "detail" in body && typeof body.detail === "string") {
        detail = body.detail;
      }
    } catch {
      // not JSON: keep the generic message
    }
    throw new ApiError(resp.status, detail);
  }
  return (await resp.json()) as T;
}

export function listRuns(limit = 20): Promise<{ runs: RunSummary[] }> {
  return request(`/runs?limit=${limit}`);
}
