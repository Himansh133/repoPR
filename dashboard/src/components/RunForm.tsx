import { useState, type FormEvent } from "react";
import { createRun } from "../api";

const field = "rounded border border-control bg-bg px-3 py-2";

/** Starts a run (this calls the real model) and opens its page. */
export function RunForm() {
  const [repo, setRepo] = useState("");
  const [issue, setIssue] = useState("");
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [allowUnpriced, setAllowUnpriced] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const number = Number(issue);
    if (!Number.isInteger(number) || number < 1) return setError("Issue number must be 1 or more.");
    if (body.trim() && !title.trim()) return setError("Give a title with the issue body, or leave both empty.");
    setSending(true);
    setError(null);
    try {
      const { id } = await createRun({
        repo: repo.trim(),
        issue: number,
        ...(title.trim() ? { issue_title: title.trim(), issue_body: body } : {}),
        allow_unpriced: allowUnpriced,
      });
      window.location.hash = `#/runs/${id}`;
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSending(false);
    }
  }

  return (
    <form onSubmit={submit} aria-labelledby="form-title" className="mb-8 rounded border border-border bg-panel p-4">
      <h2 id="form-title" className="text-lg font-semibold">
        New run
      </h2>
      <p className="mb-3 text-sm text-muted">Calls the model (costs money). Leave the title empty to fetch the issue from GitHub.</p>
      <div className="grid gap-3 sm:grid-cols-[1fr_8rem]">
        <label className="flex flex-col gap-1 text-sm">
          Repository (local git path or https://github.com/owner/repo)
          <input required value={repo} onChange={(e) => setRepo(e.target.value)} className={field} />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          Issue number
          <input required inputMode="numeric" value={issue} onChange={(e) => setIssue(e.target.value)} className={field} />
        </label>
        <label className="flex flex-col gap-1 text-sm sm:col-span-2">
          Issue title (optional)
          <input value={title} maxLength={300} onChange={(e) => setTitle(e.target.value)} className={field} />
        </label>
        <label className="flex flex-col gap-1 text-sm sm:col-span-2">
          Issue body (optional)
          <textarea value={body} maxLength={10000} rows={3} onChange={(e) => setBody(e.target.value)} className={field} />
        </label>
      </div>
      <label className="mt-3 flex items-center gap-2 text-sm">
        <input type="checkbox" checked={allowUnpriced} onChange={(e) => setAllowUnpriced(e.target.checked)} />
        Allow a run when model prices are not set
      </label>
      {error && (
        <p role="alert" className="mt-2 text-sm text-error">
          {error}
        </p>
      )}
      <button
        type="submit"
        disabled={sending}
        className="mt-3 rounded bg-primary-strong px-3 py-2 font-medium text-white hover:bg-primary-hover disabled:opacity-60"
      >
        {sending ? "Starting…" : "Start run"}
      </button>
    </form>
  );
}
