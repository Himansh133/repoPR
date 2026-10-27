import { useState, type FormEvent } from "react";
import { ApiError, request, setKey } from "../api";

export function KeyGate({ onDone }: { onDone: () => void }) {
  const [key, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setChecking(true);
    setError(null);
    try {
      await request("/runs?limit=1", {}, key.trim());
      setKey(key.trim());
      onDone();
    } catch (err) {
      setError(err instanceof ApiError && err.status === 401 ? "Wrong key." : String((err as Error).message));
    } finally {
      setChecking(false);
    }
  }

  return (
    <main className="mx-auto mt-24 max-w-sm rounded-lg border border-border bg-panel p-6">
      <h1 className="text-xl font-semibold">RepoPilot</h1>
      <form onSubmit={submit} className="mt-4 flex flex-col gap-3">
        <label htmlFor="api-key" className="text-sm text-muted">
          API key (REPOPILOT_API_KEY)
        </label>
        <input
          id="api-key"
          type="password"
          autoComplete="off"
          required
          value={key}
          onChange={(e) => setValue(e.target.value)}
          className="rounded border border-control bg-bg px-3 py-2"
        />
        {error && (
          <p role="alert" className="text-sm text-error">
            {error}
          </p>
        )}
        <button
          type="submit"
          disabled={checking || !key.trim()}
          className="rounded bg-primary-strong px-3 py-2 font-medium text-white hover:bg-primary-hover disabled:opacity-60"
        >
          {checking ? "Checking…" : "Continue"}
        </button>
      </form>
      <p className="mt-4 text-xs text-muted">Kept only in this tab and sent only as a header.</p>
    </main>
  );
}
