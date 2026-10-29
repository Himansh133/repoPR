import { useState } from "react";
import { approveRun, rejectRun } from "../api";
import type { Run } from "../types";

/** Approve (two clicks, then a draft PR) or reject a passed run. Hidden for any other run. */
export function ApprovePanel({ run, onChange }: { run: Run; onChange: () => void }) {
  const [confirming, setConfirming] = useState(false);
  const [sending, setSending] = useState(false);
  const [rejected, setRejected] = useState(false);
  const [closesIssue, setClosesIssue] = useState(false);
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);

  if (run.pr_url) {
    return (
      <p className="rounded border border-border bg-panel p-3">
        Draft PR:{" "}
        <a href={run.pr_url} target="_blank" rel="noopener noreferrer" className="text-link hover:underline">
          {run.pr_url}
        </a>
      </p>
    );
  }
  if (run.status !== "passed") return null;
  if (rejected) {
    return <p className="rounded border border-border bg-panel p-3">Rejected. Nothing was sent to GitHub.</p>;
  }
  // An approval without a PR means the PR step failed, so the buttons stay for a retry.
  if (run.decision?.decision === "rejected") {
    const { decided_at, note: why } = run.decision;
    return (
      <p className="rounded border border-border bg-panel p-3">
        Rejected on {decided_at.replace("T", " ").slice(0, 16)}. Nothing was sent to GitHub.
        {why && <span className="text-muted"> Note: {why}</span>}
      </p>
    );
  }

  async function act(action: () => Promise<unknown>, after: () => void) {
    setSending(true);
    setError(null);
    try {
      await action();
      after();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSending(false);
    }
  }

  const button = "rounded px-3 py-2 font-medium disabled:opacity-60";
  return (
    <section aria-labelledby="decision-title" className="rounded border border-border bg-panel p-3">
      <h2 id="decision-title" className="font-semibold">
        Decision
      </h2>
      {confirming ? (
        <div className="mt-2 flex flex-col gap-2">
          <p className="text-sm">
            This creates a <strong>draft</strong> pull request on {run.repo} from the tested working copy.
          </p>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={closesIssue} onChange={(e) => setClosesIssue(e.target.checked)} />
            Close issue #{run.issue_number} when merged (writes “Fixes #{run.issue_number}”)
          </label>
          <label htmlFor="approve-note" className="text-sm text-muted">
            Note (optional)
          </label>
          <input
            id="approve-note"
            value={note}
            maxLength={500}
            onChange={(e) => setNote(e.target.value)}
            className="rounded border border-control bg-bg px-3 py-2"
          />
          <div className="flex gap-2">
            <button
              type="button"
              disabled={sending}
              className={`${button} bg-primary-strong text-white hover:bg-primary-hover`}
              onClick={() => act(() => approveRun(run.id, closesIssue, note), onChange)}
            >
              {sending ? "Creating…" : "Create draft PR"}
            </button>
            <button
              type="button"
              disabled={sending}
              className={`${button} border border-control`}
              onClick={() => setConfirming(false)}
            >
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <div className="mt-2 flex gap-2">
          <button
            type="button"
            className={`${button} bg-primary-strong text-white hover:bg-primary-hover`}
            onClick={() => setConfirming(true)}
          >
            Approve…
          </button>
          <button
            type="button"
            disabled={sending}
            className={`${button} border border-control`}
            onClick={() => act(() => rejectRun(run.id, note), () => setRejected(true))}
          >
            Reject
          </button>
        </div>
      )}
      {error && (
        <p role="alert" className="mt-2 text-sm text-error">
          {error}
        </p>
      )}
    </section>
  );
}
