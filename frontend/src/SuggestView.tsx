import { useEffect, useMemo, useState } from "react";
import { api, type Game, type Preview, type Suggestion } from "./api";
import { GameRow, GameTableHead } from "./format";

function isoDaysAgo(days: number) {
  const d = new Date();
  d.setDate(d.getDate() - days);
  // Local calendar date (toISOString would shift to UTC and can show tomorrow).
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

export function SuggestView({ onQueued }: { onQueued: () => void }) {
  const [from, setFrom] = useState(isoDaysAgo(30));
  const [to, setTo] = useState(isoDaysAgo(0));
  const [band, setBand] = useState(100);
  const [maxComparison, setMaxComparison] = useState(40);
  const [suggestion, setSuggestion] = useState<Suggestion | null>(null);
  const [games, setGames] = useState<Map<number, Game>>(new Map());
  const [excluded, setExcluded] = useState<Set<number>>(new Set());
  const [preview, setPreview] = useState<Preview | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  async function propose(e?: React.FormEvent) {
    e?.preventDefault();
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      const s = await api.suggest({ target_from: from, target_to: to, rating_band: band, max_comparison: maxComparison });
      const ids = s.groups.flatMap((g) => g.game_ids);
      const list = ids.length ? await api.gamesByIds(ids) : [];
      setGames(new Map(list.map((g) => [g.id, g])));
      setExcluded(new Set());
      setSuggestion(s);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const included = useMemo(
    () => (suggestion ? suggestion.groups.flatMap((g) => g.game_ids).filter((id) => !excluded.has(id)) : []),
    [suggestion, excluded],
  );

  useEffect(() => {
    if (!included.length) return setPreview(null);
    const t = setTimeout(() => api.preview(included).then(setPreview).catch(() => setPreview(null)), 200);
    return () => clearTimeout(t);
  }, [included]);

  function toggle(id: number) {
    setExcluded((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });
  }

  async function queue() {
    if (!suggestion) return;
    setBusy(true);
    try {
      const roles: Record<number, string> = {};
      suggestion.groups.forEach((g) => g.game_ids.forEach((id) => (roles[id] = g.role)));
      const b = await api.createBatch({
        game_ids: included,
        mode: "suggested",
        roles,
        rationale: { target_from: from, target_to: to, rating_band: band, groups: suggestion.groups.map(({ key, title, reason }) => ({ key, title, reason })), removed: [...excluded] },
      });
      const s = b.summary!;
      setMessage(`Queued ${s.queued + s.requeued} game(s); ${s.already_queued} were already queued and ${s.already_analyzed} already analyzed.`);
      setSuggestion(null);
      onQueued();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="stack">
      <section className="card">
        <h2>Suggest a comparison set</h2>
        <p className="muted">
          Pick the period you want to review. The app proposes those games as targets, plus your earlier games from the same
          platform, time control and rating pool at a similar rating, so later comparisons are like for like.
        </p>
        <form className="form" onSubmit={propose}>
          <label>Target games from<input type="date" value={from} onChange={(e) => setFrom(e.target.value)} required /></label>
          <label>to<input type="date" value={to} onChange={(e) => setTo(e.target.value)} required /></label>
          <label>Rating within ±<input type="number" min={25} max={500} step={25} value={band} onChange={(e) => setBand(Number(e.target.value))} /></label>
          <label>Comparison games (max)<input type="number" min={1} max={300} value={maxComparison} onChange={(e) => setMaxComparison(Number(e.target.value))} /></label>
          <button className="primary" disabled={busy}>{busy ? "Working…" : "Suggest a set"}</button>
        </form>
        {error && <p className="error">{error}</p>}
        {message && <p className="notice notice-info">{message}</p>}
      </section>

      {suggestion && (
        <>
          {suggestion.warnings.map((w, i) => <p key={i} className="notice notice-warning">{w}</p>)}
          {suggestion.advice?.map((a, i) => (
            <p key={`a${i}`} className={`notice ${a.level === "weak" ? "notice-warning" : "notice-info"}`}><b>History specialist, {a.pool}:</b> {a.text}</p>
          ))}
          {suggestion.groups.map((g) => (
            <section key={g.key} className={`card group group-${g.role}`}>
              <div className="table-head">
                <h3>{g.title}</h3>
                <span className="muted">{g.game_ids.filter((id) => !excluded.has(id)).length} of {g.game_ids.length} included</span>
              </div>
              <p className="reason">{g.reason}</p>
              <div className="table-wrap">
                <table>
                  <GameTableHead selectable />
                  <tbody>
                    {g.game_ids.map((id) => {
                      const game = games.get(id);
                      return game ? <GameRow key={id} g={game} checked={!excluded.has(id)} onToggle={() => toggle(id)} /> : null;
                    })}
                  </tbody>
                </table>
              </div>
            </section>
          ))}
          {suggestion.groups.length > 0 && (
            <div className="action-bar">
              <span><b>{included.length}</b> games in this set</span>
              {preview && <span className="muted">{preview.will_queue} will be queued · {preview.already_queued} already queued · {preview.already_analyzed} already analyzed</span>}
              <button className="primary" disabled={busy || !preview || preview.will_queue === 0} onClick={queue}>Queue this set</button>
            </div>
          )}
        </>
      )}
    </div>
  );
}
