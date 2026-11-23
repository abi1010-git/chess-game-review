import { useEffect, useState } from "react";
import { coach, type CoachClaim, type CoachReview } from "./api";

export const PROVIDER_NAME: Record<string, string> = {
  gemini: "Gemini", groq: "Groq", cerebras: "Cerebras", mistral: "Mistral", openrouter: "OpenRouter", github: "GitHub Models",
};

const VERDICT: Record<CoachClaim["verdict"], string> = {
  worse: "Worse than usual", better: "Better than usual", similar: "Your usual pattern", mixed: "Mixed record",
  insufficient: "Not enough evidence", narrowed: "This game only",
};
const MATCH: Record<CoachClaim["match_quality"], string> = {
  exact: "same position", structure: "same pawn structure", theme: "same kind of decision", phase: "similar decisions", none: "no matches",
};

const ACTION_TEXT: Record<string, (d: Record<string, any>) => string> = {
  "tool:candidate_decisions": (d) => `Reviewer picked ${d.found} candidate decisions from the engine evidence.`,
  "model_selected": (d) => `The AI chose which ${d.plies.length} decisions to compare.`,
  "tool:engine_lines+board_features": () => "Reviewer fetched the engine's line and the board features for one decision.",
  "handoff:history": (d) => `Reviewer handed ${d.requests.length} decisions to the history specialist.`,
  "tool:search_history": (d) => `History specialist searched for ${d.feature === "exact" ? "the same position" : d.feature === "structure" ? "the same pawn structure" : d.feature === "theme" ? "the same kind of decision" : "similar decisions anywhere"}: ${d.games} game${d.games === 1 ? "" : "s"}.`,
  "request_different_search": (d) => `Match too weak, so the specialist searched again more broadly (${String(d.reason).replace(/\.$/, "")}).`,
  "handoff:reviewer_narrow_claim": () => "Still not enough comparable games, so the reviewer was asked to narrow the claim.",
  "narrowed_claims": (d) => `Reviewer limited ${d.plies.length} claim${d.plies.length === 1 ? "" : "s"} to this game only.`,
  "templates": () => "Writer drafted the standard explanations.",
  "model:write": (d) => `${PROVIDER_NAME[d.provider] ?? d.provider} rewrote the explanations in plain language.`,
  "model:write_failed": () => "No AI model could write a verified explanation, so the standard ones are kept.",
  "checked": (d) => `Verifier checked ${d.claims} claims against ${d.refs} stored moves.`,
  "dropped_claim": (d) => `Verifier dropped a claim: ${d.reason}.`,
  "recommended": () => "Study coach picked a whole game to practise this.",
  "no_recommendation": () => "Study coach found nothing that needs a whole-game study.",
  "ask_another_example": (d) => `Study coach skipped a game (${d.reason}) and looked for another.`,
};

function stepText(t: { action: string; detail: Record<string, any> }) {
  const f = ACTION_TEXT[t.action];
  return f ? f(t.detail) : null;
}

export function CoachCard({ gameId, onJump, onOpen, onStudy }: {
  gameId: number;
  onJump: (ply: number) => void;
  onOpen: (gameId: number, ply?: number) => void;
  onStudy: (gameId: number, theme: string | null) => void;
}) {
  const [review, setReview] = useState<CoachReview | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showTrace, setShowTrace] = useState(false);

  useEffect(() => {
    setReview(null);
    setLoaded(false);
    coach.review(gameId).then((r) => setReview(r.review)).catch(() => undefined).finally(() => setLoaded(true));
  }, [gameId]);

  async function run() {
    setBusy(true);
    setError(null);
    try {
      setReview((await coach.runReview(gameId)).review);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card coach">
      <div className="table-head">
        <h3>Coach review</h3>
        {review && (
          <span className={`badge ${review.mode === "model" ? "badge-ai" : ""}`}>
            {review.mode === "model" ? `Written with ${PROVIDER_NAME[review.provider ?? ""] ?? "AI"}` : "Standard explanations"}
          </span>
        )}
      </div>
      {!loaded ? <p className="muted">Loading…</p> : !review ? (
        <div className="stack-sm">
          <p className="muted">
            The coach picks the decisions that mattered most, compares each one with your earlier games, and checks every
            claim against the engine's evidence before showing it.
          </p>
          <div><button className="primary" disabled={busy} onClick={run}>{busy ? "Reviewing…" : "Review with coach"}</button></div>
        </div>
      ) : (
        <div className="stack-sm">
          <p>{review.summary}</p>
          {review.fallbacks.map((f, i) => <p key={i} className="notice notice-info small">{f}</p>)}
          <ul className="claims">
            {review.claims.map((c) => {
              const others = [...new Map(c.refs.filter((r) => r.game_id !== gameId).map((r) => [r.game_id, r])).values()];
              return (
                <li key={c.ply} className="claim">
                  <div className="decision-head">
                    <button className="link" onClick={() => onJump(c.ply)}>{c.headline}</button>
                    <span className={`badge verdict-${c.verdict === "narrowed" ? "insufficient" : c.verdict}`}>{VERDICT[c.verdict]}</span>
                  </div>
                  <p className="small">{c.text}</p>
                  <div className="muted small">
                    {c.match_quality !== "none" ? `Matched on ${MATCH[c.match_quality]}` : "No comparable decisions"}
                    {c.searches > 1 ? ` · searched ${c.searches} ways` : ""}
                    {others.length > 0 && <> · earlier games: {others.slice(0, 5).map((r, i) => (
                      <span key={r.game_id}>{i ? ", " : ""}<button className="link" onClick={() => onOpen(r.game_id, r.ply)}>#{r.game_id}</button></span>
                    ))}</>}
                  </div>
                </li>
              );
            })}
          </ul>
          {review.recommended_study && (
            <div className="notice notice-info">
              <b>Study this next:</b> {review.recommended_study.reason}{" "}
              <button className="link" onClick={() => onStudy(review.recommended_study!.game_id, review.recommended_study!.theme)}>Start the study →</button>
            </div>
          )}
          <button className="link small" onClick={() => setShowTrace((v) => !v)}>
            {showTrace ? "Hide" : "Show"} how the coach worked ({review.handoffs} handoffs, {review.tool_calls} tool calls)
          </button>
          {showTrace && (
            <ol className="trace small muted">
              {review.trace.map((t, i) => { const s = stepText(t); return s ? <li key={i}><b>{t.actor}</b> · {s}</li> : null; })}
            </ol>
          )}
        </div>
      )}
      {error && <p className="error">{error}</p>}
    </section>
  );
}
