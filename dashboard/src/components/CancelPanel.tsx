import { useState } from "react";
import { cancelRun } from "../api";
import type { Run } from "../types";

/** Stop a running run (two clicks). It ends at its next step; the timeline shows when. */
export function CancelPanel({ run }: { run: Run }) {
  const [confirming, setConfirming] = useState(false);
  const [sending, setSending] = useState(false);
  const [sent, setSent] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (run.status !== "running") return null;
  if (sent) {
    return (
      <p role="status" className="rounded border border-border bg-panel p-3">
        Cancelling… the run stops at its next step.
      </p>
    );
  }

  async function stop() {
    setSending(true);
    setError(null);
    try {
      await cancelRun(run.id);
      setSent(true);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSending(false);
    }
  }

  const button = "rounded px-3 py-2 font-medium disabled:opacity-60";
  return (
    <section aria-label="Cancel run" className="rounded border border-border bg-panel p-3">
      {confirming ? (
        <div className="flex flex-wrap items-center gap-2">
          <p className="text-sm">The step in progress finishes first, then the run ends as aborted.</p>
          <button
            type="button"
            disabled={sending}
            className={`${button} bg-primary-strong text-white hover:bg-primary-hover`}
            onClick={stop}
          >
            {sending ? "Stopping…" : "Stop this run"}
          </button>
          <button
            type="button"
            disabled={sending}
            className={`${button} border border-control`}
            onClick={() => setConfirming(false)}
          >
            Keep running
          </button>
        </div>
      ) : (
        <button type="button" className={`${button} border border-control`} onClick={() => setConfirming(true)}>
          Cancel run…
        </button>
      )}
      {error && (
        <p role="alert" className="mt-2 text-sm text-error">
          {error}
        </p>
      )}
    </section>
  );
}
