import type { Game } from "./api";

export const PROVIDER_LABEL: Record<string, string> = { lichess: "Lichess", chesscom: "Chess.com", pgn: "PGN" };

export function formatDate(g: Pick<Game, "played_at" | "date_raw">): string {
  if (g.played_at) return g.played_at.slice(0, 10);
  return g.date_raw ? `${g.date_raw} (partial)` : "Unknown date";
}

export function moves(g: Game): string {
  return `${Math.ceil(g.ply_count / 2)} moves`;
}

export function ResultBadge({ result }: { result: Game["player_result"] }) {
  const text = { win: "Win", loss: "Loss", draw: "Draw", unknown: "—" }[result];
  return <span className={`badge result-${result}`}>{text}</span>;
}

const STATUS_TEXT: Record<string, string> = {
  none: "Not analyzed",
  queued: "Queued",
  running: "Analyzing",
  done: "Analyzed",
  failed: "Failed",
  cancelled: "Cancelled",
  missing: "Not queued",
};

export function StatusBadge({ status }: { status: string }) {
  return <span className={`badge status-${status}`}>{STATUS_TEXT[status] ?? status}</span>;
}

export function GameRow({ g, checked, onToggle, onOpen }: { g: Game; checked?: boolean; onToggle?: () => void; onOpen?: (id: number) => void }) {
  return (
    <tr className={checked ? "selected" : undefined}>
      {onToggle && (
        <td className="check">
          <input type="checkbox" checked={!!checked} onChange={onToggle} aria-label={`Select game ${g.id}`} />
        </td>
      )}
      <td className="nowrap">{formatDate(g)}</td>
      <td>
        <span className={`side side-${g.player_color}`} title={`You played ${g.player_color}`} />
        {onOpen ? (
          <button className="link" onClick={() => onOpen(g.id)} title="Open the game report">vs {g.opponent_name ?? "Unknown"}</button>
        ) : (
          <>vs {g.opponent_name ?? "Unknown"}</>
        )}
        {g.opponent_rating ? <span className="muted"> ({g.opponent_rating})</span> : null}
        {g.suggested && (
          <span className="badge suggested" title={g.suggested === "target"
            ? "Suggested: one of your latest games, worth analyzing first"
            : "Suggested: an earlier game that the latest games are compared with"}>
            Suggested{g.suggested === "comparison" ? " · for comparison" : ""}
          </span>
        )}
      </td>
      <td><ResultBadge result={g.player_result} /></td>
      <td className="nowrap">{g.player_rating ?? <span className="muted">—</span>}</td>
      <td className="nowrap">
        {g.time_class}
        {g.time_control ? <span className="muted"> · {g.time_control}</span> : null}
      </td>
      <td className="nowrap">
        {g.source_url ? (
          <a href={g.source_url} target="_blank" rel="noreferrer">{PROVIDER_LABEL[g.provider] ?? g.provider}</a>
        ) : (
          PROVIDER_LABEL[g.provider] ?? g.provider
        )}
        {g.rated === false ? <span className="muted"> · casual</span> : null}
      </td>
      <td className="nowrap muted">{moves(g)}</td>
      <td className="opening" title={g.opening ?? undefined}>{g.opening ?? <span className="muted">—</span>}</td>
      <td><StatusBadge status={g.analysis_status} /></td>
    </tr>
  );
}

export function GameTableHead({ selectable, allChecked, onToggleAll }: { selectable?: boolean; allChecked?: boolean; onToggleAll?: () => void }) {
  return (
    <thead>
      <tr>
        {selectable && (
          <th className="check">
            {onToggleAll && <input type="checkbox" checked={!!allChecked} onChange={onToggleAll} aria-label="Select this page" />}
          </th>
        )}
        <th>Date</th>
        <th>Opponent</th>
        <th>Result</th>
        <th>Your rating</th>
        <th>Time control</th>
        <th>Source</th>
        <th>Length</th>
        <th>Opening</th>
        <th>Analysis</th>
      </tr>
    </thead>
  );
}
