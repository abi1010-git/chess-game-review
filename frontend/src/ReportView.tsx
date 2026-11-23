import { useEffect, useMemo, useState } from "react";
import { api2, type Comparison, type Decision, type Finding, type GameReport, type LossClaim, type Ply } from "./api";
import { Board } from "./Board";
import { CoachCard } from "./CoachCard";
import { EvalGraph } from "./charts";
import { WorkerBanner } from "./DashboardView";
import { formatDate, PROVIDER_LABEL, ResultBadge, StatusBadge } from "./format";

const PHASES = ["opening", "middlegame", "endgame"] as const;
const PHASE_LABEL = { opening: "Opening", middlegame: "Middlegame", endgame: "Endgame" };
const VERDICT_LABEL: Record<Comparison["verdict"], string> = {
  better: "Better than usual", worse: "Worse than usual", similar: "Similar to usual", mixed: "Mixed evidence",
  insufficient: "Not enough evidence",
};
const LOW_INFO: Record<string, string> = {
  forced: "only legal move", decided: "position already decided", recapture: "obvious recapture",
  obvious_capture: "taking a loose piece",
};

function label(p: { move_number: number; side: string }, san: string) {
  return `${p.move_number}${p.side === "white" ? "." : "…"} ${san}`;
}

function evalText(cp: number | null, mate: number | null) {
  if (mate !== null) return mate > 0 ? `mate in ${mate}` : `mated in ${-mate}`;
  return `${(cp ?? 0) >= 0 ? "+" : ""}${((cp ?? 0) / 100).toFixed(1)}`;
}

export function ReportView({ gameId, initialPly, onOpen, onBack, onStudy }: {
  gameId: number; initialPly?: number; onOpen: (gameId: number, ply?: number) => void; onBack: () => void;
  onStudy: (gameId: number, theme: string | null) => void;
}) {
  const [data, setData] = useState<GameReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [ply, setPly] = useState<number>(initialPly ?? 0);
  const [showBest, setShowBest] = useState(false);
  const [queueing, setQueueing] = useState(false);

  const load = () => api2.report(gameId).then(setData).catch((e) => setError(e.message));
  useEffect(() => {
    setData(null);
    setPly(initialPly ?? 0);
    setShowBest(false);
    load();
  }, [gameId, initialPly]);
  const status = data?.game.analysis_status;
  useEffect(() => {
    if (status !== "queued" && status !== "running") return;
    const t = setInterval(load, 4000);
    return () => clearInterval(t);
  }, [status]);

  const r = data?.report;
  const findingsByPly = useMemo(() => {
    const m = new Map<number, Finding[]>();
    r?.findings.forEach((f) => m.set(f.ply, [...(m.get(f.ply) ?? []), f]));
    return m;
  }, [r]);

  useEffect(() => {
    if (!r) return;
    const onKey = (e: KeyboardEvent) => {
      if ((e.target as HTMLElement)?.tagName === "INPUT") return;
      if (e.key === "ArrowLeft") setPly((p) => Math.max(0, p - 1));
      if (e.key === "ArrowRight") setPly((p) => Math.min(r.plies.length - 1, p + 1));
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [r]);

  if (error) return <p className="error">{error}</p>;
  if (!data) return <p className="muted">Loading…</p>;
  const g = data.game;

  async function queue() {
    setQueueing(true);
    try {
      await api2.queueGames([g.id]);
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setQueueing(false);
    }
  }

  const header = (
    <section className="card">
      <div className="table-head">
        <button className="link" onClick={onBack}>← Back</button>
        <h2>
          <span className={`side side-${g.player_color}`} />vs {g.opponent_name ?? "Unknown"}
          {g.opponent_rating ? <span className="muted"> ({g.opponent_rating})</span> : null}
        </h2>
        <ResultBadge result={g.player_result} />
        <StatusBadge status={g.analysis_status} />
      </div>
      <div className="muted small">
        {formatDate(g)} · {g.time_class}{g.time_control ? ` (${g.time_control})` : ""} · {PROVIDER_LABEL[g.provider] ?? g.provider}
        {g.player_rating ? ` · you ${g.player_rating}` : ""} · you played {g.player_color}
        {g.opening ? ` · ${g.opening}` : ""}
        {g.source_url && <> · <a href={g.source_url} target="_blank" rel="noreferrer">original game</a></>}
      </div>
    </section>
  );

  if (!r) {
    return (
      <div className="stack">
        {header}
        <WorkerBanner s={data} />
        <section className="card empty">
          {status === "queued" || status === "running" ? (
            <p><span className="spinner" style={{ display: "inline-block" }} /> {status === "running" ? "Analyzing this game…" : "Queued for analysis."} The report appears here when it's done.</p>
          ) : (
            <>
              <p>
                {data?.game.outdated_version
                  ? `This game was analyzed with an older version of the analysis (${data.game.outdated_version}); the current version is newer, so it needs analyzing again`
                  : "This game hasn't been analyzed yet"}
                {status === "failed" ? " (the last attempt failed)" : ""}.
              </p>
              <button className="primary" disabled={queueing} onClick={queue}>Analyze this game</button>
            </>
          )}
        </section>
      </div>
    );
  }

  const p = r.plies[Math.min(ply, r.plies.length - 1)];
  const flipped = g.player_color === "black";
  const fs = findingsByPly.get(p.ply) ?? [];
  const decision = r.decisions.find((d) => d.ply === p.ply);

  return (
    <div className="stack">
      {header}
      <Verdict r={r} onJump={setPly} />
      {r.loss && <LossCard loss={r.loss} onJump={setPly} />}
      <CoachCard gameId={g.id} onJump={setPly} onOpen={onOpen} onStudy={onStudy} />

      <div className="phase-cards">
        {PHASES.map((ph) => {
          const o = r.phases[ph];
          return (
            <section key={ph} className="card phase-card">
              <h3>{PHASE_LABEL[ph]}</h3>
              <div className="headline">{o.headline}</div>
              <div className="muted small">
                {o.decisions} decision{o.decisions === 1 ? "" : "s"}
                {o.avg_drop !== null ? ` · avg. ${o.avg_drop} win-% given away` : ""}
                {o.mistakes ? ` · ${o.mistakes} mistake${o.mistakes === 1 ? "" : "s"}` : ""}
              </div>
            </section>
          );
        })}
      </div>

      <section className="card">
        <div className="review">
          <div className="board-col">
            <Board fen={p.fen_before} flipped={flipped} lastMove={p.uci} arrow={showBest && p.best_uci !== p.uci ? p.best_uci : null} />
            <div className="board-controls">
              <button onClick={() => setPly(0)} aria-label="First move">⏮</button>
              <button onClick={() => setPly(Math.max(0, p.ply - 1))} aria-label="Previous move">◀</button>
              <span className="muted small">Before {label(p, p.san)}</span>
              <button onClick={() => setPly(Math.min(r.plies.length - 1, p.ply + 1))} aria-label="Next move">▶</button>
              <button onClick={() => setPly(r.plies.length - 1)} aria-label="Last move">⏭</button>
            </div>
            <label className="inline">
              <input type="checkbox" checked={showBest} onChange={(e) => setShowBest(e.target.checked)} /> Show the engine's move as an arrow
            </label>
          </div>
          <div className="detail-col">
            <MoveDetail p={p} findings={fs} />
            {decision?.comparison && <ComparisonBlock d={decision} onOpen={onOpen} />}
            <MoveList plies={r.plies} current={p.ply} onSelect={setPly} findingsByPly={findingsByPly} />
          </div>
        </div>
        <EvalGraph plies={r.plies} current={p.ply} onSelect={setPly} />
      </section>

      <section className="card">
        <h3>Findings</h3>
        {r.findings.length === 0 && <p className="muted">No findings with enough evidence in this game.</p>}
        {PHASES.map((ph) => {
          const list = r.findings.filter((f) => f.phase === ph);
          if (!list.length) return null;
          return (
            <div key={ph} className="finding-group">
              <h4>{PHASE_LABEL[ph]}</h4>
              {list.map((f) => <FindingRow key={f.id} f={f} p={r.plies[f.ply]} onJump={setPly} />)}
            </div>
          );
        })}
      </section>

      <section className="card">
        <h3>Compared with your other games</h3>
        <p className="hint">Each key decision is matched to your own earlier decisions: the same position first, then the same material and pawn structure, then the same kind of decision in the same phase.</p>
        {r.decisions.map((d) => (
          <div key={d.ply} className="decision">
            <div className="decision-head">
              <button className="link" onClick={() => setPly(d.ply)}>{label(d, d.move)}</button>
              <span className={`badge cls-${d.classification}`}>{d.classification}</span>
              <span className="muted small">{PHASE_LABEL[d.phase]}</span>
              {d.comparison && <span className={`badge verdict-${d.comparison.verdict}`}>{VERDICT_LABEL[d.comparison.verdict]}</span>}
            </div>
            {d.comparison && <p className="small">{d.comparison.text}</p>}
          </div>
        ))}
      </section>

      <p className="hint">
        {r.run.engine}, analysis {r.run.analysis_version}: {Number(r.run.settings.first_nodes).toLocaleString()} nodes per position,
        {" "}{Number(r.run.settings.deep_nodes).toLocaleString()} for the {r.run.summary.deep_checks} deeper checks.
        {r.run.summary.unstable ? ` ${r.run.summary.unstable} verdict(s) changed between passes and are marked uncertain.` : ""}
      </p>
    </div>
  );
}

function Verdict({ r, onJump }: { r: NonNullable<GameReport["report"]>; onJump: (ply: number) => void }) {
  const v = r.verdict;
  const s = r.run.summary;
  return (
    <section className="card verdict-card">
      <div className="verdict-stats">
        <div><span className="big">{s.accuracy ?? "—"}</span><span className="muted small">accuracy</span></div>
        <div><span className="big">{s.meaningful_decisions}</span><span className="muted small">real decisions</span></div>
        <div><span className="big">{(s.classifications.mistake ?? 0) + (s.classifications.blunder ?? 0)}</span><span className="muted small">mistakes</span></div>
      </div>
      <div className="verdict-text">
        {v.text && <p>{v.text}</p>}
        {v.strongest && (
          <p>
            <b>{v.kind === "full" ? "Key moment" : "Strongest observation"}: {v.strongest.title}.</b> {v.strongest.summary}{" "}
            <ConfidenceBadge c={v.strongest.confidence} />{" "}
            <button className="link" onClick={() => onJump(v.strongest!.ply)}>Show</button>
          </p>
        )}
        {s.low_info > 0 && <p className="hint">{s.low_info} of your moves were forced, obvious, or played in a decided position and are not judged.</p>}
      </div>
    </section>
  );
}

function LossCard({ loss, onJump }: { loss: NonNullable<NonNullable<GameReport["report"]>["loss"]>; onJump: (ply: number) => void }) {
  const claims = [loss.earlier, loss.decisive, loss.missed].filter(Boolean) as LossClaim[];
  const title = { earlier: "Earlier decision that narrowed your options", decisive: "The decisive mistake", missed: "A concrete chance you missed" };
  return (
    <section className="card">
      <h3>Why this game was lost</h3>
      <p>{loss.text}</p>
      {claims.length > 0 && (
        <ol className="claims">
          {claims.sort((a, b) => a.ply - b.ply).map((c) => (
            <li key={c.role}>
              <div><b>{title[c.role]}:</b> <button className="link" onClick={() => onJump(c.ply)}>{c.move_number}. {c.move}</button>
                {c.best_move && <span className="muted"> (better: {c.best_move})</span>}
                <span className="muted small"> · {c.win_before}% → {c.win_after}%</span>
                {!c.stable && <> <ConfidenceBadge c="uncertain" /></>}
              </div>
              <div className="small">{c.basis.title ? `${c.basis.title}. ` : ""}{c.basis.summary}</div>
              {c.recurrence && <div className="small muted">{c.recurrence.text}</div>}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

function ConfidenceBadge({ c }: { c: string }) {
  const text = { clear: "clear", likely: "likely", uncertain: "uncertain" }[c] ?? c;
  return <span className={`badge conf-${c}`} title="How firmly the engine evidence supports this">{text}</span>;
}

function MoveDetail({ p, findings }: { p: Ply; findings: Finding[] }) {
  const isBest = p.best_uci === p.uci;
  return (
    <div className="move-detail">
      <div className="decision-head">
        <b>{label(p, p.san)}</b>
        {p.is_player ? <span className={`badge cls-${p.classification}`}>{p.classification}</span> : <span className="muted small">opponent's move</span>}
        {p.low_info_reason && <span className="muted small">not judged: {LOW_INFO[p.low_info_reason] ?? p.low_info_reason}</span>}
      </div>
      {p.is_player ? (
        <div className="small">
          Your expected score: {p.win_best.toFixed(0)}% with the best move{isBest ? "" : `, ${p.win_played.toFixed(0)}% after ${p.san}`}
          <span className="muted"> ({evalText(p.played_cp, p.played_mate)} for you after the move)</span>
        </div>
      ) : (
        <div className="small">
          Your expected score after their move: {p.win_played.toFixed(0)}%
          <span className="muted"> ({evalText(p.played_cp, p.played_mate)} for you)</span>
          {!isBest && p.best_san && <span className="muted">. Their strongest option was {p.best_san}, which would have left you at {p.win_best.toFixed(0)}%.</span>}
        </div>
      )}
      {p.is_player && !isBest && p.best_san && <div className="small">Engine's choice: <b>{p.best_san}</b> ({evalText(p.best_cp, p.best_mate)})</div>}
      {p.reply_line.length > 0 && <div className="small muted">Expected continuation: {p.reply_line.slice(0, 6).join(" ")}</div>}
      {p.deep_checked && <div className="small muted">Checked again at a deeper budget{p.stable ? "; the verdict held." : "; the verdict changed, so treat it as uncertain."}</div>}
      {findings.map((f) => (
        <div key={f.id} className={`finding finding-${f.polarity}`}>
          <b>{f.title}</b> <ConfidenceBadge c={f.confidence} /> <span className="muted small">{f.kind}</span>
          <div className="small">{f.summary}</div>
        </div>
      ))}
      {p.candidates.length > 1 && (
        <details>
          <summary className="small">Engine candidates</summary>
          <ul className="small cands">
            {p.candidates.map((c) => (
              <li key={c.uci}><b>{c.san}</b> {evalText(c.cp, c.mate)} · {c.win.toFixed(0)}% · <span className="muted">{c.line.slice(0, 6).join(" ")}</span></li>
            ))}
          </ul>
          <p className="hint">Scores are from the side to move's point of view, first pass.</p>
        </details>
      )}
    </div>
  );
}

function ComparisonBlock({ d, onOpen }: { d: Decision; onOpen: (gameId: number, ply?: number) => void }) {
  const c = d.comparison!;
  const st = c.stats;
  return (
    <div className="comparison">
      <div className="decision-head">
        <span className={`badge verdict-${c.verdict}`}>{VERDICT_LABEL[c.verdict]}</span>
        {c.theme_label && <span className="muted small">{c.theme_label}</span>}
      </div>
      <p className="small">{c.text}</p>
      {st.examples > 0 && (
        <div className="small muted">
          Sample: {st.examples} decision{st.examples === 1 ? "" : "s"} from {st.games} game{st.games === 1 ? "" : "s"} ·
          {" "}exact position {st.by_match.exact}, same structure {st.by_match.structure}, same theme {st.by_match.theme}
        </div>
      )}
      {c.change && c.change.direction !== "steady" && (
        <div className="small">
          Over time: {c.change.direction === "improving" ? "improving" : "slipping"} (mistakes in {(c.change.earlier_mistake_rate * 100).toFixed(0)}% of the earlier {c.change.earlier_n} vs {(c.change.later_mistake_rate * 100).toFixed(0)}% of the later {c.change.later_n}).
        </div>
      )}
      {d.recurrence && <div className="small muted">{d.recurrence.text}</div>}
      {c.examples.length > 0 && (
        <details>
          <summary className="small">Earlier examples</summary>
          <ul className="small examples">
            {c.examples.map((e) => (
              <li key={`${e.game_id}-${e.ply}`}>
                <button className="link" onClick={() => onOpen(e.game_id, e.ply)}>
                  {e.played_at ? e.played_at.slice(0, 10) : "undated"} · {e.move_number}{e.side === "white" ? "." : "…"} {e.move}
                </button>
                <span className={`badge cls-${e.classification}`}>{e.classification}</span>
                <span className="muted"> {e.match} match · {e.result}</span>
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

function MoveList({ plies, current, onSelect, findingsByPly }: { plies: Ply[]; current: number; onSelect: (n: number) => void; findingsByPly: Map<number, Finding[]> }) {
  const rows: { n: number; w?: Ply; b?: Ply }[] = [];
  for (const p of plies) {
    let row = rows.find((r) => r.n === p.move_number);
    if (!row) rows.push((row = { n: p.move_number }));
    if (p.side === "white") row.w = p; else row.b = p;
  }
  const cell = (p?: Ply) => p ? (
    <button className={`mv ${p.ply === current ? "on" : ""} ${p.is_player ? `cls-${p.classification}` : "opp"}`} onClick={() => onSelect(p.ply)}>
      {p.san}{findingsByPly.has(p.ply) ? " •" : ""}
    </button>
  ) : <span />;
  return (
    <div className="movelist">
      {rows.map((r) => (
        <div key={r.n} className="mrow"><span className="muted">{r.n}.</span>{cell(r.w)}{cell(r.b)}</div>
      ))}
    </div>
  );
}

function FindingRow({ f, p, onJump }: { f: Finding; p: Ply; onJump: (ply: number) => void }) {
  return (
    <div className={`finding finding-${f.polarity}`}>
      <button className="link" onClick={() => onJump(f.ply)}>{label(p, p.san)}</button> <b>{f.title}</b>{" "}
      <ConfidenceBadge c={f.confidence} /> <span className="muted small">{f.kind}</span>
      <div className="small">{f.summary}</div>
      {f.evidence.best_line && f.evidence.best_line.length > 0 && (
        <div className="small muted">Engine line: {f.evidence.best_line.slice(0, 6).join(" ")}</div>
      )}
    </div>
  );
}
