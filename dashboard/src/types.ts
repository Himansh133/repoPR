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
