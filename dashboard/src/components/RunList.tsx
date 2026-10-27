import { useEffect, useState } from "react";
import { listRuns } from "../api";
import type { RunSummary } from "../types";
import { StatusBadge } from "./StatusBadge";

export function RunList() {
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    listRuns()
      .then((data) => live && setRuns(data.runs))
      .catch((err: Error) => live && setError(err.message));
    return () => {
      live = false;
    };
  }, []);

  if (error) return <p role="alert" className="text-error">Could not load runs: {error}</p>;
  if (runs === null) return <p className="text-muted">Loading runs…</p>;
  if (runs.length === 0) return <p className="text-muted">No runs yet.</p>;

  return (
    <table className="w-full text-left text-sm">
      <caption className="mb-2 text-left text-lg font-semibold text-fg">Runs</caption>
      <thead className="text-muted">
        <tr className="border-b border-border">
          {["Run", "Date", "Status", "Issue", "Model", "Tokens", "Cost", "PR"].map((h) => (
            <th key={h} scope="col" className="py-2 pr-4 font-medium">
              {h}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {runs.map((r) => (
          <tr key={r.id} className="border-b border-border hover:bg-elevated">
            <td className="py-2 pr-4">
              <a href={`#/runs/${r.id}`} className="text-link underline-offset-2 hover:underline">
                #{r.id}
              </a>
            </td>
            <td className="py-2 pr-4">{r.started_at.slice(0, 10)}</td>
            <td className="py-2 pr-4">
              <StatusBadge status={r.status} />
            </td>
            <td className="py-2 pr-4">
              <span className="text-muted">{r.repo}#{r.issue_number}</span> {r.issue_title}
            </td>
            <td className="py-2 pr-4">{r.model.split("/").pop()}</td>
            <td className="py-2 pr-4">{r.input_tokens + r.output_tokens}</td>
            <td className="py-2 pr-4">{r.cost === null ? "unknown" : r.cost + (r.cost_estimated ? " (est.)" : "")}</td>
            <td className="py-2 pr-4">
              {r.pr_url ? (
                <a href={r.pr_url} target="_blank" rel="noopener noreferrer" className="text-link hover:underline">
                  draft PR
                </a>
              ) : (
                <span className="text-muted">—</span>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
