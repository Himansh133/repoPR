import { useEffect, useState } from "react";
import { getKey, setUnauthorizedHandler } from "./api";
import { KeyGate } from "./components/KeyGate";
import { RunDetail } from "./components/RunDetail";
import { RunForm } from "./components/RunForm";
import { RunList } from "./components/RunList";

function useHash(): string {
  const [hash, setHash] = useState(window.location.hash);
  useEffect(() => {
    const update = () => setHash(window.location.hash);
    window.addEventListener("hashchange", update);
    return () => window.removeEventListener("hashchange", update);
  }, []);
  return hash;
}

export function App() {
  const [hasKey, setHasKey] = useState(() => getKey() !== null);
  const hash = useHash();

  useEffect(() => {
    setUnauthorizedHandler(() => setHasKey(false));
    return () => setUnauthorizedHandler(null);
  }, []);

  if (!hasKey) return <KeyGate onDone={() => setHasKey(true)} />;

  const runId = /^#\/runs\/(\d+)$/.exec(hash)?.[1];
  return (
    <div className="mx-auto max-w-6xl p-6">
      <header className="mb-6 flex items-center justify-between">
        <a href="#/" className="text-xl font-semibold">
          RepoPilot
        </a>
      </header>
      <main>
        {runId ? (
          <RunDetail key={runId} runId={Number(runId)} />
        ) : (
          <>
            <RunForm />
            <RunList />
          </>
        )}
      </main>
    </div>
  );
}
