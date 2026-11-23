import { useEffect, useState } from "react";
import { me, Me } from "./api";

/** Shows whose data this is and lets the visitor try the sample history, download or delete their data. */
export function SpaceBar({ version, onChanged }: { version: number; onChanged: () => void }) {
  const [info, setInfo] = useState<Me | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const load = () => me.get().then(setInfo).catch(() => setInfo(null));
  useEffect(() => { load(); }, [version]);
  if (!info || info.mode !== "guest") return null;

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try { await fn(); onChanged(); await load(); } catch (e) { setError((e as Error).message); } finally { setBusy(false); setConfirming(false); }
  };
  return (
    <div className="space-bar">
      <span>
        {info.games === 0
          ? "New here? Try the whole workflow in one click with a sample history, or import your own games."
          : `Your private space holds ${info.games} game${info.games === 1 ? "" : "s"}. It is tied to this browser, and deleted after ${info.retention_days} idle days.`}
      </span>
      <span className="space-actions">
        {info.games === 0 && info.sample_available && (
          <button className="primary" disabled={busy} onClick={() => run(async () => { await me.loadSample(); window.location.hash = "#home"; })}>
            {busy ? "Loading…" : "Try a sample history"}
          </button>
        )}
        {info.has_space && info.games > 0 && <a href="/api/me/export" className="linkbtn">Download my data</a>}
        {info.has_space && !confirming && <button disabled={busy} onClick={() => setConfirming(true)}>Delete my data</button>}
        {confirming && (
          <>
            <span>Delete everything for good?</span>
            <button className="danger" disabled={busy} onClick={() => run(async () => { await me.remove(); window.location.hash = "#import"; })}>Yes, delete</button>
            <button onClick={() => setConfirming(false)}>Cancel</button>
          </>
        )}
      </span>
      {error && <span className="error">{error}</span>}
    </div>
  );
}
