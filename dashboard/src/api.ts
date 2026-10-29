import { createSseParser, type SseMessage } from "./sse";
import type { Execution, NewRun, Patch, Run, RunSummary } from "./types";

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

async function send(path: string, init: RequestInit, key: string | null): Promise<Response> {
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
  return resp;
}

export async function request<T>(path: string, init: RequestInit = {}, key = getKey()): Promise<T> {
  const resp = await send(path, init, key);
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

export const getRun = (id: number) => request<Run>(`/runs/${id}`);
export const getDiff = (id: number) => request<{ patches: Patch[] }>(`/runs/${id}/diff`);
export const getTests = (id: number) => request<{ executions: Execution[] }>(`/runs/${id}/tests`);

export function createRun(body: NewRun): Promise<{ id: number }> {
  return request("/runs", { method: "POST", body: JSON.stringify(body) });
}

export function approveRun(id: number, closesIssue: boolean, note: string): Promise<{ pr_url: string }> {
  const body = { confirm: true, closes_issue: closesIssue, note };
  return request(`/runs/${id}/approve`, { method: "POST", body: JSON.stringify(body) });
}

export function rejectRun(id: number, note: string): Promise<{ decision: string }> {
  return request(`/runs/${id}/reject`, { method: "POST", body: JSON.stringify({ note }) });
}

export function cancelRun(id: number): Promise<{ cancelling: boolean }> {
  return request(`/runs/${id}/cancel`, { method: "POST" });
}

/** Streams a run's events (live, or replayed if finished) until the server ends the stream. */
export async function streamEvents(
  id: number,
  onMessage: (message: SseMessage) => void,
  signal: AbortSignal,
): Promise<void> {
  const resp = await send(`/runs/${id}/events`, { signal }, getKey());
  if (!resp.ok || !resp.body) throw new ApiError(resp.status, `Could not load events (${resp.status})`);
  const feed = createSseParser(onMessage);
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    feed(decoder.decode(value, { stream: true }));
  }
}
