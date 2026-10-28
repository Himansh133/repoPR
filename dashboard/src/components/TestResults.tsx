import { useEffect, useState } from "react";
import { getTests } from "../api";
import type { Execution } from "../types";

export function TestResults({ runId }: { runId: number }) {
  const [executions, setExecutions] = useState<Execution[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getTests(runId)
      .then((d) => setExecutions(d.executions))
      .catch((e: Error) => setError(e.message));
  }, [runId]);

  if (error) return <p role="alert" className="text-error">Could not load test results: {error}</p>;
  if (executions === null) return <p className="text-muted">Loading test results…</p>;
  if (executions.length === 0) return <p className="text-muted">No tests were run.</p>;

  let patchNo = 0;
  return (
    <ul className="flex flex-col gap-3">
      {executions.map((e, i) => {
        const label = e.kind === "baseline" ? "Before the patch" : `After patch ${++patchNo}`;
        const tone = e.status === "passed" ? "text-success" : "text-error";
        return (
          <li key={i} className="rounded border border-border bg-panel p-3">
            <p>
              <span className="font-medium">{label}:</span> <span className={`font-medium ${tone}`}>{e.status}</span>{" "}
              <span className="text-muted">({e.duration_s.toFixed(1)} s)</span>
            </p>
            {e.reason && <p className="text-sm text-muted">{e.reason}</p>}
            {e.failed_tests.length > 0 && (
              <ul className="mt-1 list-disc pl-5 font-mono text-sm">
                {e.failed_tests.map((t) => (
                  <li key={t}>{t}</li>
                ))}
              </ul>
            )}
            {e.truncated && <p className="text-sm text-muted">Output was truncated.</p>}
          </li>
        );
      })}
    </ul>
  );
}
