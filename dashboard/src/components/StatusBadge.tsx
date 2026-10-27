import type { RunStatus } from "../types";

const TONE: Record<RunStatus, string> = {
  passed: "text-success",
  running: "text-warning",
  inconclusive: "text-warning",
  failed: "text-error",
  aborted: "text-error",
  error: "text-error",
};

// The word is always shown, so status never depends on color alone.
export function StatusBadge({ status }: { status: RunStatus }) {
  return <span className={`font-medium ${TONE[status] ?? "text-muted"}`}>{status}</span>;
}
