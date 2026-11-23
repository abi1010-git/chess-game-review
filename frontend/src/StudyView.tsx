import { useEffect, useMemo, useState } from "react";
import { coach, type Study, type StudyCheckpoint } from "./api";
import { Board } from "./Board";
import { PROVIDER_NAME } from "./CoachCard";
import { PROVIDER_LABEL, ResultBadge } from "./format";

function moveLabel(ply: number, san: string) {
  return `${Math.floor(ply / 2) + 1}${ply % 2 === 0 ? "." : "…"} ${san}`;
}

/** A whole game to study. Guided mode stops at each checkpoint until the player reveals it. */
export function StudyView({ studyId, onBack, onOpenReport }: { studyId: number; onBack: () => void; onOpenReport: (gameId: number) => void }) {
  const [st, setSt] = useState<Study | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [ply, setPly] = useState(0);
  const [guided, setGuided] = useState(true);

  useEffect(() => {
    coach.study(studyId).then((s) => { setSt(s); setPly(s.start_ply); }).catch((e) => setError(e.message));
  }, [studyId]);

  // In guided mode you can't step past a checkpoint you haven't revealed yet.
  const limit = useMemo(() => {
    if (!st) return 0;
    if (!guided) return st.moves.length;
    const next = st.checkpoints.find((c) => !c.revealed);
    return next ? next.ply : st.moves.length;
  }, [st, guided]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA") return;
      if (e.key === "ArrowLeft") setPly((p) => Math.max(0, p - 1));
      if (e.key === "ArrowRight") setPly((p) => Math.min(limit, p + 1));
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [limit]);

  if (error) return <p className="error">{error}</p>;
  if (!st) return <p className="muted">Loading…</p>;
  const cp = st.checkpoints.find((c) => c.ply === ply);
  const flipped = st.game.color === "black";
  const lastPly = ply - 1;
  const done = st.checkpoints.filter((c) => c.revealed).length;

  return (
    <div className="stack">
      <section className="card">
        <div className="table-head">
          <button className="link" onClick={onBack}>← Back</button>
          <h2>{st.title}</h2>
          <ResultBadge result={st.game.result} />
          {st.mode === "model" && <span className="badge badge-ai">Written with {PROVIDER_NAME[st.provider ?? ""] ?? "AI"}</span>}
        </div>
        <div className="muted small">
          vs {st.game.opponent ?? "Unknown"}{st.game.opponent_rating ? ` (${st.game.opponent_rating})` : ""} · {st.game.time_class} ·{" "}
          {PROVIDER_LABEL[st.game.provider] ?? st.game.provider} · you played {st.game.color}
          {st.game.played_at ? ` · ${st.game.played_at.slice(0, 10)}` : ""}
        </div>
        <p>{st.rationale}</p>
        <p className="small"><b>Goal:</b> {st.objective}</p>
      </section>

      <section className="card">
        <div className="review">
          <div className="board-col">
            <Board fen={st.positions[ply]} flipped={flipped} lastMove={null} />
            <div className="board-controls">
              <button onClick={() => setPly(0)} aria-label="Start of the game">⏮</button>
              <button onClick={() => setPly(Math.max(0, ply - 1))} aria-label="Previous move">◀</button>
              <span className="muted small">{ply === 0 ? "Starting position" : `After ${moveLabel(lastPly, st.moves[lastPly])}`}</span>
              <button onClick={() => setPly(Math.min(limit, ply + 1))} disabled={ply >= limit} aria-label="Next move">▶</button>
              <button onClick={() => setPly(limit)} disabled={ply >= limit} aria-label="Next checkpoint">⏭</button>
            </div>
            <label className="inline">
              <input type="checkbox" checked={!guided} onChange={(e) => setGuided(!e.target.checked)} /> Explore freely (skip the checkpoints)
            </label>
            <p className="hint">{done} of {st.checkpoints.length} checkpoints revealed.</p>
          </div>
          <div className="detail-col">
            {cp ? (
              <CheckpointPanel key={cp.index} st={st} cp={cp} onChange={setSt} onNext={() => setPly((p) => Math.min(p + 1, st.moves.length))} />
            ) : (
              <div className="stack-sm">
                <h3>Play through the game</h3>
                <p className="muted small">
                  {ply < limit || !guided
                    ? "Step forward with ▶ or the arrow keys. Think about each of your moves before you see it."
                    : "Checkpoint ahead."}
                </p>
                {guided && st.checkpoints.some((c) => !c.revealed && c.ply >= ply) && (
                  <button className="primary" onClick={() => setPly(limit)}>Go to the next checkpoint</button>
                )}
                {st.checkpoints.every((c) => c.revealed) && (
                  <p className="notice notice-info">
                    You've worked through every checkpoint.{" "}
                    <button className="link" onClick={() => onOpenReport(st.game_id)}>Open the full engine report →</button>
                  </p>
                )}
              </div>
            )}
            <MoveStrip st={st} ply={ply} onSelect={setPly} />
          </div>
        </div>
      </section>
    </div>
  );
}

function MoveStrip({ st, ply, onSelect }: { st: Study; ply: number; onSelect: (p: number) => void }) {
  const cps = new Set(st.checkpoints.map((c) => c.ply));
  return (
    <div className="movelist">
      {st.moves.slice(0, ply).map((san, i) => (
        <button key={i} className={`mv ${i + 1 === ply ? "on" : ""} ${i % 2 === (st.game.color === "white" ? 1 : 0) ? "opp" : ""}`} onClick={() => onSelect(i + 1)}>
          {cps.has(i) ? "◆ " : ""}{moveLabel(i, san)}
        </button>
      ))}
    </div>
  );
}

function CheckpointPanel({ st, cp, onChange, onNext }: { st: Study; cp: StudyCheckpoint; onChange: (s: Study) => void; onNext: () => void }) {
  const [plans, setPlans] = useState(cp.notes.plans ?? "");
  const [move, setMove] = useState(cp.notes.move ?? "");
  const [reply, setReply] = useState(cp.notes.reply ?? "");
  const [text, setText] = useState(cp.notes.text ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const dirty = plans !== (cp.notes.plans ?? "") || move !== (cp.notes.move ?? "") || reply !== (cp.notes.reply ?? "") || text !== (cp.notes.text ?? "");

  async function save(): Promise<boolean> {
    setBusy(true);
    setError(null);
    try {
      const s = await coach.saveNotes(st.id, cp.index, { plans, move: move || null, reply: reply || null, text });
      onChange(s);
      setSaved(true);
      return true;
    } catch (e) {
      setError((e as Error).message);
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function reveal() {
    if (dirty && !(await save())) return;
    setBusy(true);
    try {
      onChange(await coach.reveal(st.id, cp.index));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const a = cp.answer;
  return (
    <div className="stack-sm checkpoint">
      <h3>◆ Checkpoint {cp.index + 1} of {st.checkpoints.length}</h3>
      <p>{cp.prompt}</p>
      <label className="field">Candidate plans
        <textarea rows={2} value={plans} onChange={(e) => { setPlans(e.target.value); setSaved(false); }} disabled={cp.revealed}
          placeholder="e.g. attack the king, trade queens, push the passed pawn" />
      </label>
      <div className="field-row">
        <label className="field">Your move
          <input value={move} onChange={(e) => { setMove(e.target.value); setSaved(false); }} disabled={cp.revealed} placeholder="e.g. Nf3" />
        </label>
        <label className="field">Opponent's reply
          <input value={reply} onChange={(e) => { setReply(e.target.value); setSaved(false); }} disabled={cp.revealed} placeholder="e.g. d5" />
        </label>
      </div>
      <label className="field">Notes
        <textarea rows={2} value={text} onChange={(e) => { setText(e.target.value); setSaved(false); }} disabled={cp.revealed} />
      </label>
      {error && <p className="error small">{error}</p>}
      {!cp.revealed ? (
        <div className="row-buttons">
          <button disabled={busy || !dirty} onClick={save}>{saved && !dirty ? "Saved" : "Save notes"}</button>
          <button className="primary" disabled={busy} onClick={reveal}>Reveal the answer</button>
        </div>
      ) : a && (
        <div className="answer stack-sm">
          <p>
            In the game you played <b>{a.played}</b> ({a.verdict}).{" "}
            {a.best && a.best !== a.played ? <>The engine's choice was <b>{a.best}</b>.</> : "That was the engine's choice too."}
          </p>
          <p className="muted small">
            Expected score before the move {a.win_before.toFixed(0)}%, after your game move {a.win_after.toFixed(0)}%.
            {a.best_line.length > 1 ? ` Engine line: ${a.best_line.slice(0, 6).join(" ")}.` : ""}
          </p>
          {cp.feedback && (
            <div className="notice notice-info">
              <b>Coach{cp.feedback.source === "model" ? ` (${PROVIDER_NAME[cp.feedback.provider ?? ""] ?? "AI"})` : ""}:</b> {cp.feedback.text}
            </div>
          )}
          <div><button className="primary" onClick={onNext}>Continue the game ▶</button></div>
        </div>
      )}
    </div>
  );
}
