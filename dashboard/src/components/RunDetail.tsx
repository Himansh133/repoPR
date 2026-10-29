import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from "react";
import { ApiError, getRun } from "../api";
import type { Run } from "../types";
import { ApprovePanel } from "./ApprovePanel";
import { CancelPanel } from "./CancelPanel";
import { DiffView } from "./DiffView";
import { StatusBadge } from "./StatusBadge";
import { TestResults } from "./TestResults";
import { Timeline } from "./Timeline";

const TABS = ["Timeline", "Diff", "Tests", "Report"] as const;
type Tab = (typeof TABS)[number];

export function RunDetail({ runId }: { runId: number }) {
  const [run, setRun] = useState<Run | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("Timeline");
  const tabRefs = useRef<(HTMLButtonElement | null)[]>([]);

  const load = useCallback(() => {
    getRun(runId)
      .then(setRun)
      .catch((e: Error) => setError(e instanceof ApiError && e.status === 404 ? `No run with id ${runId}.` : e.message));
  }, [runId]);
  useEffect(load, [load]);

  // WAI-ARIA tabs: arrow keys move between tabs.
  function onKeyDown(event: KeyboardEvent, index: number) {
    const step = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
    if (!step) return;
    const next = (index + step + TABS.length) % TABS.length;
    setTab(TABS[next]);
    tabRefs.current[next]?.focus();
  }

  if (error) return <p role="alert" className="text-error">{error}</p>;
  if (!run) return <p className="text-muted">Loading run…</p>;

  return (
    <section aria-labelledby="run-title" className="flex flex-col gap-4">
      <a href="#/" className="text-link hover:underline">
        ← All runs
      </a>
      <header>
        <h1 id="run-title" className="text-xl font-semibold">
          Run #{run.id}: {run.issue_title}
        </h1>
        <p className="text-muted">
          {run.repo}#{run.issue_number} · {run.model}
        </p>
        <p className="mt-1">
          Status: <StatusBadge status={run.status} />
          {run.reason && <span className="text-muted"> ({run.reason})</span>} · Repairs: {run.repairs_used} · Tokens:{" "}
          {run.input_tokens + run.output_tokens} · Cost:{" "}
          {run.cost === null ? "unknown" : run.cost + (run.cost_estimated ? " (estimated)" : "")}
        </p>
      </header>
      <CancelPanel run={run} />
      <ApprovePanel run={run} onChange={load} />
      <div role="tablist" aria-label="Run details" className="flex gap-1 border-b border-border">
        {TABS.map((t, i) => (
          <button
            key={t}
            ref={(el) => {
              tabRefs.current[i] = el;
            }}
            role="tab"
            id={`tab-${t}`}
            aria-selected={tab === t}
            aria-controls={`panel-${t}`}
            tabIndex={tab === t ? 0 : -1}
            onClick={() => setTab(t)}
            onKeyDown={(e) => onKeyDown(e, i)}
            className={`px-3 py-2 ${tab === t ? "border-b-2 border-primary text-fg" : "text-muted hover:text-fg"}`}
          >
            {t}
          </button>
        ))}
      </div>
      <div role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`} tabIndex={0}>
        {tab === "Timeline" && <Timeline runId={run.id} onEnd={(s) => s !== run.status && load()} />}
        {tab === "Diff" && <DiffView runId={run.id} />}
        {tab === "Tests" && <TestResults runId={run.id} />}
        {tab === "Report" && (
          <pre className="overflow-x-auto whitespace-pre-wrap rounded bg-panel p-3 font-mono text-sm">
            {run.report ?? "No report yet."}
          </pre>
        )}
      </div>
    </section>
  );
}
