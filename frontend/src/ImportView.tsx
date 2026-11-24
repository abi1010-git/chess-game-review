import { useEffect, useRef, useState } from "react";
import { api, ApiError, type ImportRun, type NameInspection } from "./api";

type Source = "lichess" | "chesscom" | "pgn";

const ACTIVE = new Set(["queued", "running"]);
const STATUS_TEXT: Record<ImportRun["status"], string> = {
  queued: "Waiting",
  running: "Importing…",
  completed: "Done",
  completed_with_issues: "Done, with notes",
  partial: "Stopped early",
  failed: "Failed",
};

export function ImportView({ onImported, onNext }: { onImported: () => void; onNext: () => void }) {
  const [source, setSource] = useState<Source>("lichess");
  const [runs, setRuns] = useState<ImportRun[]>([]);
  const [error, setError] = useState<string | null>(null);
  const wasActive = useRef<Set<number>>(new Set());

  async function refresh() {
    try {
      const list = await api.imports();
      setRuns(list);
      // Tell the app when a run we saw running has finished, so the history refreshes.
      const nowActive = new Set(list.filter((r) => ACTIVE.has(r.status)).map((r) => r.id));
      for (const id of wasActive.current) if (!nowActive.has(id)) onImported();
      wasActive.current = nowActive;
    } catch (e) {
      setError((e as Error).message);
    }
  }

  useEffect(() => {
    refresh();
  }, []);

  const anyActive = runs.some((r) => ACTIVE.has(r.status));
  useEffect(() => {
    if (!anyActive) return;
    const t = setInterval(refresh, 1500);
    return () => clearInterval(t);
  }, [anyActive]);

  function started(run: ImportRun) {
    setError(null);
    wasActive.current.add(run.id);
    setRuns((r) => [run, ...r.filter((x) => x.id !== run.id)]);
    refresh();
  }

  const doneWithGames = runs.some((r) => !ACTIVE.has(r.status) && r.imported + r.duplicates > 0);

  return (
    <div className="stack">
      {runs.length === 0 && (
        <section className="card intro">
          <h2>See how your chess decisions change over time</h2>
          <ol className="steps">
            <li><b>Import</b> your games from Lichess, Chess.com or a PGN file.</li>
            <li><b>Pick games</b> to analyze. Stockfish checks every move you made.</li>
            <li><b>Read the reports</b>: what went wrong, how it compares with your earlier games, and what to study next.</li>
          </ol>
        </section>
      )}
      <section className="card">
        <h2>Import games</h2>
        <div className="segmented" role="tablist">
          {(["lichess", "chesscom", "pgn"] as Source[]).map((s) => (
            <button key={s} role="tab" aria-selected={source === s} className={source === s ? "on" : ""} onClick={() => setSource(s)}>
              {s === "lichess" ? "Lichess username" : s === "chesscom" ? "Chess.com username" : "Upload PGN"}
            </button>
          ))}
        </div>
        {source === "pgn" ? (
          <PgnForm onStarted={started} onError={setError} />
        ) : (
          <AccountForm key={source} provider={source} onStarted={started} onError={setError} />
        )}
        {error && <p className="error">{error}</p>}
      </section>

      {doneWithGames && !anyActive && (
        <section className="card next-step">
          <p><b>Games imported.</b> Next, choose which games to analyze. The app marks the ones it suggests.</p>
          <button className="primary" onClick={onNext}>Choose games to analyze →</button>
        </section>
      )}

      <section className="card">
        <h2>Recent imports</h2>
        {runs.length === 0 ? <p className="muted">No imports yet.</p> : runs.map((r) => <RunCard key={r.id} run={r} />)}
      </section>
    </div>
  );
}

function AccountForm({ provider, onStarted, onError }: { provider: "lichess" | "chesscom"; onStarted: (r: ImportRun) => void; onError: (m: string) => void }) {
  const [username, setUsername] = useState("");
  const [since, setSince] = useState("");
  const [until, setUntil] = useState("");
  const [maxGames, setMaxGames] = useState(300);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      onStarted(await api.importAccount(provider, { username: username.trim(), since: since || undefined, until: until || undefined, max_games: maxGames }));
    } catch (err) {
      onError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="form" onSubmit={submit}>
      <label>
        Username
        <input value={username} onChange={(e) => setUsername(e.target.value)} required maxLength={50} pattern="[A-Za-z0-9_\-]+" placeholder={provider === "lichess" ? "Lichess username" : "Chess.com username"} />
      </label>
      <label>
        From <span className="muted">(optional)</span>
        <input type="date" value={since} onChange={(e) => setSince(e.target.value)} />
      </label>
      <label>
        To <span className="muted">(optional)</span>
        <input type="date" value={until} onChange={(e) => setUntil(e.target.value)} />
      </label>
      <label>
        Most recent games
        <input type="number" min={1} max={2000} value={maxGames} onChange={(e) => setMaxGames(Number(e.target.value))} />
      </label>
      <button className="primary" disabled={busy || !username.trim()}>{busy ? "Starting…" : "Import public games"}</button>
      <p className="hint">Only public games are available. Variants other than standard chess are skipped for now.</p>
    </form>
  );
}

function PgnForm({ onStarted, onError }: { onStarted: (r: ImportRun) => void; onError: (m: string) => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [info, setInfo] = useState<NameInspection | null>(null);
  const [chosen, setChosen] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);

  async function pick(f: File | null) {
    setFile(f);
    setInfo(null);
    setChosen(new Set());
    if (!f) return;
    setBusy(true);
    try {
      const result = await api.inspectPgn(f);
      setInfo(result);
      if (result.suggested_player) setChosen(new Set([result.suggested_player]));
    } catch (err) {
      onError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  function toggle(name: string) {
    setChosen((c) => {
      const n = new Set(c);
      if (n.has(name)) n.delete(name);
      else n.add(name);
      return n;
    });
  }

  async function submit() {
    if (!file) return;
    setBusy(true);
    try {
      onStarted(await api.importPgn(file, [...chosen]));
      setFile(null);
      setInfo(null);
    } catch (err) {
      onError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="form">
      <label>
        PGN file (one or many games, up to 5 MB)
        <input type="file" accept=".pgn,text/plain,application/x-chess-pgn" onChange={(e) => pick(e.target.files?.[0] ?? null)} />
      </label>
      {info && (
        <div className="names">
          <p>
            Found <b>{info.games_found}</b> readable game{info.games_found === 1 ? "" : "s"}
            {info.invalid_games ? <>, and <b>{info.invalid_games}</b> that will be skipped with a reason</> : null}.
            {" "}
            {info.needs_choice ? "Which player is you? Choose every name you have used." : "We think this player is you:"}
          </p>
          <div className="chips">
            {info.names.slice(0, 30).map((n) => (
              <label key={n.name} className={`chip ${chosen.has(n.name) ? "on" : ""}`}>
                <input type="checkbox" checked={chosen.has(n.name)} onChange={() => toggle(n.name)} />
                {n.name} <span className="muted">· {n.games}</span>
              </label>
            ))}
          </div>
          <button className="primary" disabled={busy || chosen.size === 0} onClick={submit}>
            {busy ? "Uploading…" : `Import games for ${[...chosen].join(", ") || "…"}`}
          </button>
        </div>
      )}
      {busy && !info && <p className="muted">Reading file…</p>}
    </div>
  );
}

function RunCard({ run }: { run: ImportRun }) {
  const [open, setOpen] = useState(false);
  const active = ACTIVE.has(run.status);
  return (
    <div className={`run run-${run.status}`}>
      <div className="run-head">
        <b>{run.label}</b>
        <span className={`badge run-status-${run.status}`}>{active && <span className="spinner" />}{STATUS_TEXT[run.status]}</span>
      </div>
      <div className="run-counts">
        <span><b>{run.imported}</b> imported</span>
        <span><b>{run.duplicates}</b> already had</span>
        <span><b>{run.skipped}</b> skipped</span>
        <span className="muted">{run.seen} read</span>
      </div>
      {run.notices.map((n, i) => (
        <p key={i} className={`notice notice-${n.level}`}>{n.message}</p>
      ))}
      {run.skips.length > 0 && (
        <button className="link" onClick={() => setOpen(!open)}>
          {open ? "Hide skipped games" : `Why were ${run.skipped} game${run.skipped === 1 ? "" : "s"} skipped?`}
        </button>
      )}
      {open && (
        <ul className="skips">
          {run.skips.map((s, i) => (
            <li key={i}><span>{s.label}</span> — {s.reason}</li>
          ))}
        </ul>
      )}
    </div>
  );
}
