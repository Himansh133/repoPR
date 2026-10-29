// Shapes returned by app/api.py.
export type RunStatus = "running" | "passed" | "failed" | "inconclusive" | "aborted" | "error";

export interface RunSummary {
  id: number;
  started_at: string;
  finished_at: string | null;
  repo: string;
  issue_number: number;
  issue_title: string;
  model: string;
  status: RunStatus;
  reason: string | null;
  branch: string | null;
  repairs_used: number;
  steps: number;
  input_tokens: number;
  output_tokens: number;
  cost: string | null;
  cost_estimated: boolean;
  pr_url: string | null;
}

export interface Decision {
  decision: "approved" | "rejected";
  decided_at: string;
  note: string;
}

export interface Run extends RunSummary {
  report: string | null;
  decision?: Decision | null; // the latest approve/reject, from the approvals table
}

export interface Patch {
  commit_sha: string;
  files: string[];
  added: number;
  removed: number;
  diff: string;
}

export interface Execution {
  kind: "baseline" | "after_patch";
  status: string;
  exit_code: number | null;
  duration_s: number;
  failed_tests: string[];
  truncated: boolean;
  reason: string | null;
}

export interface AgentEvent {
  ts: number;
  state: string | null;
  kind: string;
  data: Record<string, unknown>;
}

export interface NewRun {
  repo: string;
  issue: number;
  issue_title?: string;
  issue_body?: string;
  allow_unpriced: boolean;
}
