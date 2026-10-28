import { useEffect, useState } from "react";
import { getDiff } from "../api";
import type { Patch } from "../types";

function lineClass(line: string): string {
  if (line.startsWith("+++") || line.startsWith("---") || line.startsWith("diff ") || line.startsWith("index ")) {
    return "text-muted";
  }
  if (line.startsWith("@@")) return "text-primary";
  if (line.startsWith("+")) return "bg-success/10 text-success";
  if (line.startsWith("-")) return "bg-error/10 text-error";
  return "";
}

export function DiffView({ runId }: { runId: number }) {
  const [patches, setPatches] = useState<Patch[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getDiff(runId)
      .then((d) => setPatches(d.patches))
      .catch((e: Error) => setError(e.message));
  }, [runId]);

  if (error) return <p role="alert" className="text-error">Could not load the diff: {error}</p>;
  if (patches === null) return <p className="text-muted">Loading diff…</p>;
  if (patches.length === 0) return <p className="text-muted">No patch was applied.</p>;

  return (
    <div className="flex flex-col gap-4">
      {patches.map((p, i) => (
        <section key={p.commit_sha} aria-label={`Patch ${i + 1}`} className="rounded border border-border bg-panel">
          <h3 className="border-b border-border px-3 py-2 text-sm">
            Patch {i + 1}: {p.files.join(", ")} <span className="text-success">+{p.added}</span>{" "}
            <span className="text-error">−{p.removed}</span>
          </h3>
          <pre className="overflow-x-auto p-3 font-mono text-sm leading-6">
            {p.diff.split("\n").map((line, n) => (
              <div key={n} className={lineClass(line)}>
                {line || " "}
              </div>
            ))}
          </pre>
        </section>
      ))}
    </div>
  );
}
