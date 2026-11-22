// Small hand-built SVG charts: the game's expected-score curve and the monthly accuracy trend.
import { useRef, useState } from "react";
import type { Dashboard, Ply } from "./api";

const W = 720;

function moveLabel(p: Pick<Ply, "move_number" | "side" | "san">) {
  return `${p.move_number}${p.side === "white" ? "." : "…"} ${p.san}`;
}

/** Your expected score (0-100%) after every move. Click a point to jump the board there. */
export function EvalGraph({ plies, current, onSelect }: { plies: Ply[]; current: number; onSelect: (ply: number) => void }) {
  const H = 170, padL = 34, padR = 8, padT = 10, padB = 22;
  const ref = useRef<SVGSVGElement>(null);
  const [hover, setHover] = useState<number | null>(null);
  if (plies.length === 0) return null;
  const n = plies.length;
  const x = (i: number) => padL + (n === 1 ? 0 : (i / (n - 1)) * (W - padL - padR));
  const y = (v: number) => padT + (1 - v / 100) * (H - padT - padB);
  const pts = plies.map((p, i) => [x(i), y(p.win_played)] as const);
  const line = pts.map(([a, b], i) => `${i ? "L" : "M"}${a.toFixed(1)},${b.toFixed(1)}`).join(" ");
  const area = `${line} L${x(n - 1)},${y(50)} L${x(0)},${y(50)} Z`;
  const marks = plies.filter((p) => p.is_player && (p.classification === "mistake" || p.classification === "blunder"));

  function pick(clientX: number) {
    const r = ref.current?.getBoundingClientRect();
    if (!r) return null;
    const px = ((clientX - r.left) / r.width) * W;
    const i = Math.round(((px - padL) / (W - padL - padR)) * (n - 1));
    return Math.max(0, Math.min(n - 1, i));
  }
  const h = hover ?? null;
  const hp = h !== null ? plies[h] : null;
  return (
    <div className="viz eval-graph">
      <svg ref={ref} viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Your expected score after each move"
        onPointerMove={(e) => setHover(pick(e.clientX))} onPointerLeave={() => setHover(null)}
        onClick={(e) => { const i = pick(e.clientX); if (i !== null) onSelect(i); }}>
        {[0, 50, 100].map((v) => (
          <g key={v}>
            <line x1={padL} x2={W - padR} y1={y(v)} y2={y(v)} className={v === 50 ? "axis-mid" : "grid"} />
            <text x={padL - 6} y={y(v) + 4} textAnchor="end" className="tick">{v}%</text>
          </g>
        ))}
        <path d={area} className="eval-area" />
        <path d={line} className="eval-line" />
        <line x1={x(current)} x2={x(current)} y1={padT} y2={H - padB} className="cursor" />
        {marks.map((p) => (
          <circle key={p.ply} cx={x(p.ply)} cy={y(p.win_played)} r={5}
            className={p.classification === "blunder" ? "mk-critical" : "mk-serious"} />
        ))}
        {h !== null && <line x1={x(h)} x2={x(h)} y1={padT} y2={H - padB} className="crosshair" />}
        <text x={padL} y={H - 6} className="tick">start</text>
        <text x={W - padR} y={H - 6} textAnchor="end" className="tick">move {plies[n - 1].move_number}</text>
      </svg>
      {hp && (
        <div className="tooltip" style={{ left: `${(x(h!) / W) * 100}%` }}>
          <strong>{hp.win_played.toFixed(0)}%</strong> <span>after {moveLabel(hp)}</span>
          {hp.is_player && hp.classification !== "best" && hp.classification !== "good" && (
            <span className="muted"> · {hp.classification}</span>
          )}
        </div>
      )}
      <div className="viz-key">
        <span><i className="dot mk-serious" /> Mistake</span>
        <span><i className="dot mk-critical" /> Blunder</span>
        <span className="muted">Above 50% you were better. Click the chart to jump to a move.</span>
      </div>
    </div>
  );
}

/** Average accuracy per month. Months with fewer than three games are drawn hollow. */
export function TrendChart({ trends }: { trends: Dashboard["trends"] }) {
  const rows = trends.filter((t) => t.accuracy !== null);
  const [hover, setHover] = useState<number | null>(null);
  if (rows.length === 0) return <p className="muted">No dated analyzed games yet.</p>;
  const H = 150, padL = 34, padR = 12, padT = 10, padB = 24;
  const vals = rows.map((r) => r.accuracy!);
  const lo = Math.max(0, Math.floor((Math.min(...vals) - 5) / 10) * 10);
  const hi = Math.min(100, Math.ceil((Math.max(...vals) + 5) / 10) * 10);
  const n = rows.length;
  const x = (i: number) => (n === 1 ? (W + padL) / 2 : padL + (i / (n - 1)) * (W - padL - padR));
  const y = (v: number) => padT + (1 - (v - lo) / (hi - lo || 1)) * (H - padT - padB);
  const d = rows.map((r, i) => `${i ? "L" : "M"}${x(i)},${y(r.accuracy!)}`).join(" ");
  const hr = hover !== null ? rows[hover] : null;
  return (
    <div className="viz">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Average accuracy by month">
        {[lo, (lo + hi) / 2, hi].map((v) => (
          <g key={v}>
            <line x1={padL} x2={W - padR} y1={y(v)} y2={y(v)} className="grid" />
            <text x={padL - 6} y={y(v) + 4} textAnchor="end" className="tick">{Math.round(v)}</text>
          </g>
        ))}
        {n > 1 && <path d={d} className="trend-line" />}
        {rows.map((r, i) => (
          <g key={r.month} onPointerEnter={() => setHover(i)} onPointerLeave={() => setHover(null)}>
            <rect x={x(i) - 18} y={padT} width={36} height={H - padT - padB} fill="transparent" />
            <circle cx={x(i)} cy={y(r.accuracy!)} r={5} className={r.thin ? "trend-pt thin" : "trend-pt"} />
            {(i === 0 || i === n - 1 || n <= 8) && (
              <text x={x(i)} y={H - 6} textAnchor={n > 1 && i === 0 ? "start" : n > 1 && i === n - 1 ? "end" : "middle"} className="tick">{r.month}</text>
            )}
          </g>
        ))}
      </svg>
      {hr && (
        <div className="tooltip" style={{ left: `${(x(hover!) / W) * 100}%` }}>
          <strong>{hr.accuracy}</strong> <span>accuracy, {hr.month}</span>
          <span className="muted"> · {hr.games} game{hr.games === 1 ? "" : "s"}, {hr.mistakes_per_game} mistakes/game</span>
        </div>
      )}
      <div className="viz-key">
        <span><i className="dot trend-pt" /> 3+ games</span>
        <span><i className="dot trend-pt thin" /> fewer than 3 games: read with care</span>
      </div>
    </div>
  );
}
