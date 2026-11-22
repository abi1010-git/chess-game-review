import type { Tab } from "./App";
import { useEffect, useState } from "react";
import { api, api2, type Batch, type QueueSummary } from "./api";
import { WorkerBanner } from "./DashboardView";

const ORDER = ["done", "running", "queued", "failed", "cancelled"];

export function QueueView({ refreshKey, onGo }: { refreshKey: number; onGo: (t: Tab) => void }) {
  const [batches, setBatches] = useState<Batch[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [queue, setQueue] = useState<QueueSummary | null>(null);

  const load = () => {
    api.batches().then(setBatches).catch((e) => setError(e.message));
    api2.queue().then(setQueue).catch(() => setQueue(null));
  };
  useEffect(() => {
    load();
  }, [refreshKey]);

  const active = batches?.some((b) => (b.progress.counts.queued ?? 0) + (b.progress.counts.running ?? 0) > 0);
  useEffect(() => {
    if (!active) return;
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
  }, [active]);

  async function cancel(id: number) {
    await api.cancelBatch(id);
    load();
  }

  return (
    <div className="stack">
      {queue && <WorkerBanner s={queue} />}
      {queue?.worker_available && (
        <p className="notice notice-info">
          {queue.workers} analysis worker{queue.workers === 1 ? "" : "s"} running ({queue.engine}). Each game takes a few minutes. Finished games appear in <button className="link" onClick={() => onGo("home")}>Your progress</button> and in <button className="link" onClick={() => onGo("history")}>Pick games</button>, where you can open each report.
        </p>
      )}
      {error && <p className="error">{error}</p>}
      {batches?.length === 0 && (
        <section className="card empty">
          <p>Nothing queued yet. Choose games to analyze first.</p>
          <button className="primary" onClick={() => onGo("history")}>Pick games</button>
        </section>
      )}
      {batches?.map((b) => {
        const total = b.progress.total || 1;
        const open = (b.progress.counts.queued ?? 0) + (b.progress.counts.running ?? 0);
        return (
          <section key={b.id} className="card">
            <div className="table-head">
              <h3>Batch #{b.id} · {b.mode === "suggested" ? "Suggested set" : "Chosen games"}</h3>
              <span className="muted">{new Date(b.created_at + "Z").toLocaleString()} · analysis {b.analysis_version}</span>
              {open > 0 && <button className="link danger" onClick={() => cancel(b.id)}>Cancel remaining</button>}
            </div>
            <div className="progress" aria-label="Batch progress">
              {ORDER.map((k) => (b.progress.counts[k] ? <div key={k} className={`seg seg-${k}`} style={{ width: `${(100 * b.progress.counts[k]) / total}%` }} /> : null))}
            </div>
            <div className="run-counts">
              {ORDER.filter((k) => b.progress.counts[k]).map((k) => <span key={k}><b>{b.progress.counts[k]}</b> {k}</span>)}
              <span className="muted">{b.progress.total} games</span>
            </div>
          </section>
        );
      })}
    </div>
  );
}
