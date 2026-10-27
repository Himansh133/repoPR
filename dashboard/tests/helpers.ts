import { vi } from "vitest";
import type { RunSummary } from "../src/types";

export function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

/** Stub fetch with one handler; returns the mock so tests can inspect calls. */
export function stubFetch(handler: (url: string, init: RequestInit) => Response | Promise<Response>) {
  const mock = vi.fn<(url: string, init: RequestInit) => Promise<Response>>(async (url, init) =>
    handler(url, init),
  );
  vi.stubGlobal("fetch", mock);
  return mock;
}

export function run(over: Partial<RunSummary> = {}): RunSummary {
  return {
    id: 1, started_at: "2026-10-27T21:00:00+00:00", finished_at: "2026-10-27T21:00:20+00:00",
    repo: "octo/demo", issue_number: 7, issue_title: "Crash on empty cart",
    model: "deepseek-ai/DeepSeek-V4-Flash", status: "passed", reason: null,
    branch: "repopilot/issue-7", repairs_used: 0, steps: 5, input_tokens: 6000,
    output_tokens: 700, cost: "0.0011", cost_estimated: false, pr_url: null, ...over,
  };
}
