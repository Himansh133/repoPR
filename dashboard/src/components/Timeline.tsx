import { useEffect, useState } from "react";
import { streamEvents } from "../api";
import type { AgentEvent } from "../types";

function summary(data: Record<string, unknown>): string {
  const text = Object.entries(data)
    .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : String(v)}`)
    .join(" ");
  return text.length > 140 ? text.slice(0, 139) + "…" : text;
}

/** Streams the run's events; calls `onEnd` with the final status when the stream ends. */
export function Timeline({ runId, onEnd }: { runId: number; onEnd?: (status: string) => void }) {
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [finalStatus, setFinalStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setEvents([]);
    setFinalStatus(null);
    streamEvents(
      runId,
      ({ event, data }) => {
        if (event === "end") {
          const status = String((data as { status?: unknown }).status ?? "unknown");
          setFinalStatus(status);
          onEnd?.(status);
        } else {
          setEvents((list) => [...list, data as AgentEvent]);
        }
      },
      controller.signal,
    ).catch((e: Error) => {
      if (!controller.signal.aborted) setError(e.message);
    });
    return () => controller.abort();
    // Only a new run id restarts the stream; a new onEnd callback from the parent must not.
  }, [runId]);

  return (
    <div>
      <p className="mb-2 text-sm text-muted" role="status">
        {error ? "" : finalStatus ? `Finished: ${finalStatus}` : "Streaming events…"}
      </p>
      {error && (
        <p role="alert" className="text-error">
          Could not load events: {error}
        </p>
      )}
      <ol aria-label="Agent events" className="flex flex-col gap-1 font-mono text-sm">
        {events.map((e, i) => (
          <li key={i} className="rounded bg-panel px-3 py-1">
            <span className="text-muted">{new Date(e.ts * 1000).toLocaleTimeString()}</span>{" "}
            <span className="font-medium text-primary">{e.kind}</span>
            {e.state && <span className="text-muted"> [{e.state}]</span>} <span>{summary(e.data ?? {})}</span>
          </li>
        ))}
      </ol>
    </div>
  );
}
