import { useEffect, useState } from "react";
import { api2, coach, type Dashboard, type SavedStudy, type StudyPick, type ThemeItem } from "./api";
import { TrendChart } from "./charts";
import { PROVIDER_LABEL, ResultBadge } from "./format";
import type { Tab } from "./App";

const PHASES = ["opening", "middlegame", "endgame"] as const;
const PHASE_LABEL = { opening: "Opening", middlegame: "Middlegame", endgame: "Endgame" };
const STATUS_LABEL: Record<ThemeItem["status"], string> = {
  recurring: "Recurring", emerging: "Occasional", single: "Once", strength: "Strength",
};

export function WorkerBanner({ s }: { s: { worker_available: boolean; worker_error: string | null; engine: string | null; workers?: number } }) {
  if (s.worker_available) return null;
  return (
    <p className={`notice ${s.worker_error ? "notice-error" : "notice-warning"}`}>
      {s.worker_error
        ? `The analysis worker could not start: ${s.worker_error}`
        : "The analysis engine isn't responding right now, so queued games are waiting. On the public site it usually restarts within a minute; if you run the app yourself, start it with: python -m app.worker (in backend/)."}
    </p>
  );
}

export function DashboardView({ refreshKey, onOpen, onGo, onStudy, onResume }: {
  refreshKey: number; onOpen: (gameId: number, ply?: number) => void; onGo: (t: Tab) => void;
  onStudy: (gameId: number, theme: string | null) => void; onResume: (studyId: number) => void;
}) {
  const [d, setD] = useState<Dashboard | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = () => api2.dashboard().then(setD).catch((e) => setError(e.message));
  useEffect(() => { load(); }, [refreshKey]);
  const busy = d && d.coverage.queued + d.coverage.running > 0;
  useEffect(() => {
    if (!busy) return;
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [busy]);

  if (error) return <p className="error">{error}</p>;
  if (!d) return <p className="muted">Loading…</p>;
  const c = d.coverage;
  const focus = d.recurring.find((r) => r.status !== "single") ?? d.recurring[0];

  return (
    <div className="stack">
      {(d.coverage.queued + d.coverage.running > 0 || d.worker_error) && <WorkerBanner s={d} />}
      <div className="home">
        <section className="card panel">
          <div className="table-head">
            <h2>How You've Been Playing</h2>
            <span className="muted">
              {c.analyzed} of {c.games} games analyzed
              {c.queued + c.running > 0 ? ` · ${c.running} running, ${c.queued} queued` : ""}
              {c.failed ? ` · ${c.failed} failed` : ""}
            </span>
          </div>
          {c.outdated > 0 && (
            <p className="notice notice-info">
              {c.outdated} game{c.outdated === 1 ? " was" : "s were"} analyzed with an older version of the analysis
              (now {d.analysis_version}) and {c.outdated === 1 ? "isn't" : "aren't"} counted here. Analyze {c.outdated === 1 ? "it" : "them"} again from <b>Pick games</b>.
            </p>
          )}
          {c.analyzed === 0 ? (
            <div className="empty">
              <p>No analyzed games yet. Your trends, recurring mistakes and strengths appear here once a few games are analyzed.</p>
              <button className="primary" onClick={() => onGo(c.games === 0 ? "import" : "history")}>{c.games === 0 ? "Import games" : "Pick games to analyze"}</button>
            </div>
          ) : (
            <>
              <h3>Accuracy by month</h3>
              <TrendChart trends={d.trends} />

              <h3 className="gap">By phase</h3>
              <div className="table-wrap">
                <table>
                  <thead><tr><th>Phase</th><th>Decisions</th><th>Games</th><th>Avg. win-% given away</th><th>Mistake rate</th></tr></thead>
                  <tbody>
                    {PHASES.map((ph) => {
                      const p = d.phases[ph];
                      return (
                        <tr key={ph}>
                          <td>{PHASE_LABEL[ph]}</td>
                          <td>{p?.decisions ?? 0}</td>
                          <td>{p?.games ?? 0}</td>
                          <td>{p?.avg_drop ?? <span className="muted">—</span>}</td>
                          <td>{p?.mistake_rate != null ? `${(p.mistake_rate * 100).toFixed(1)}%` : <span className="muted">—</span>}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              <p className="hint">Forced moves, obvious recaptures and already-decided positions are left out.</p>

              <h3 className="gap">Recurring findings</h3>
              {d.recurring.length === 0 && <p className="muted">No mistakes with clear evidence yet.</p>}
              <ul className="themes">
                {d.recurring.map((t) => <ThemeRow key={t.theme} t={t} onOpen={onOpen} />)}
              </ul>
              {d.strengths.length > 0 && (
                <>
                  <h3 className="gap">What's going well</h3>
                  <ul className="themes">{d.strengths.map((t) => <ThemeRow key={t.theme} t={t} onOpen={onOpen} />)}</ul>
                </>
              )}
              {d.uncertain_findings > 0 && (
                <p className="hint">{d.uncertain_findings} finding{d.uncertain_findings === 1 ? " is" : "s are"} marked uncertain because the engine's first and deeper checks disagreed.</p>
              )}

              <h3 className="gap">Recent games</h3>
              <div className="table-wrap">
                <table>
                  <thead><tr><th>Date</th><th>Opponent</th><th>Result</th><th>Accuracy</th><th>Mistakes</th><th>Main finding</th></tr></thead>
                  <tbody>
                    {d.recent.map((g) => (
                      <tr key={g.game_id} className="clickable" onClick={() => onOpen(g.game_id)}>
                        <td className="nowrap">{g.played_at ? g.played_at.slice(0, 10) : g.date_raw ?? "—"}</td>
                        <td>
                          <span className={`side side-${g.color}`} />
                          <button className="link" onClick={(e) => { e.stopPropagation(); onOpen(g.game_id); }}>
                            vs {g.opponent ?? "Unknown"}
                          </button>
                          <span className="muted"> · {g.time_class}, {PROVIDER_LABEL[g.provider] ?? g.provider}</span>
                        </td>
                        <td><ResultBadge result={g.result} /></td>
                        <td>{g.accuracy ?? <span className="muted">—</span>}</td>
                        <td>{g.short ? <span className="muted">short game</span> : g.mistakes}</td>
                        <td className="opening">{g.top_finding ? g.top_finding.title : <span className="muted">—</span>}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </section>

        <StudyPanel refreshKey={refreshKey} analyzed={c.analyzed} focus={focus} onOpen={onOpen} onStudy={onStudy} onResume={onResume} />
      </div>
    </div>
  );
}

function ThemeRow({ t, onOpen }: { t: ThemeItem; onOpen: (gameId: number, ply?: number) => void }) {
  const ex = t.examples[0];
  return (
    <li className="theme">
      <div className="theme-head">
        <span className={`badge theme-${t.status}`}>{STATUS_LABEL[t.status]}</span>
        <b>{t.label}</b>
        <span className="muted">{PHASE_LABEL[t.phase]}</span>
      </div>
      <div className="muted small">
        {t.occurrences}× in {t.games} game{t.games === 1 ? "" : "s"} (wins {t.by_result.win ?? 0}, draws {t.by_result.draw ?? 0}, losses {t.by_result.loss ?? 0})
        {t.uncertain ? ` · ${t.uncertain} uncertain` : ""}
        {t.opportunities ? ` · went wrong in ${(t.opportunities.mistake_rate * 100).toFixed(0)}% of ${t.opportunities.decisions} similar decisions` : ""}
        {ex && <> · <button className="link" onClick={() => onOpen(ex.game_id, ex.ply)}>latest example</button></>}
      </div>
    </li>
  );
}

function StudyPanel({ refreshKey, analyzed, focus, onOpen, onStudy, onResume }: {
  refreshKey: number; analyzed: number; focus: ThemeItem | undefined;
  onOpen: (gameId: number, ply?: number) => void; onStudy: (gameId: number, theme: string | null) => void; onResume: (studyId: number) => void;
}) {
  const [data, setData] = useState<{ picks: StudyPick[]; saved: SavedStudy[] } | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (analyzed === 0) return;
    coach.studies().then(setData).catch((e) => setError(e.message));
  }, [refreshKey, analyzed]);

  return (
    <section className="card panel">
      <h2>What to Study Next</h2>
      {analyzed === 0 ? (
        <p className="muted">Study suggestions appear once a few games are analyzed.</p>
      ) : error ? <p className="error">{error}</p> : !data ? <p className="muted">Picking games…</p> : (
        <div className="stack-sm">
          {focus && (
            <p className="small">
              Your most frequent issue is <b>{focus.label.toLowerCase()}</b>: {focus.occurrences} time{focus.occurrences === 1 ? "" : "s"} in {focus.games} game{focus.games === 1 ? "" : "s"}.{" "}
              <button className="link" onClick={() => onOpen(focus.examples[0].game_id, focus.examples[0].ply)}>Latest example</button>
            </p>
          )}
          {data.picks.length === 0 && <p className="muted">No game stands out for study yet. Analyze a few more games.</p>}
          {data.picks.map((p) => (
            <div key={p.game_id} className="pick">
              <div className="theme-head"><span className="badge">{p.theme_label}</span><ResultBadge result={p.result} /></div>
              <b>vs {p.opponent ?? "Unknown"}</b> <span className="muted small">· {p.time_class}{p.played_at ? ` · ${p.played_at.slice(0, 10)}` : ""}</span>
              <p className="small">{p.reason}</p>
              <p className="muted small">Start from move {p.start_move}. The answers stay hidden until you ask for them.</p>
              <button className="primary" onClick={() => onStudy(p.game_id, p.theme)}>Study this game</button>
            </div>
          ))}
          {data.saved.length > 0 && (
            <>
              <h3 className="gap">Your studies</h3>
              <ul className="themes">
                {data.saved.map((s) => (
                  <li key={s.id}>
                    <button className="link" onClick={() => onResume(s.id)}>{s.title}</button>
                    <span className="muted small"> · {s.revealed} of {s.checkpoints} checkpoints done</span>
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}
    </section>
  );
}
