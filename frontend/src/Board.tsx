// Read-only SVG chessboard drawn from a FEN, with optional move highlight and arrow.

const GLYPH: Record<string, string> = { k: "♚", q: "♛", r: "♜", b: "♝", n: "♞", p: "♟" };
const FILES = "abcdefgh";

function parseFen(fen: string): (string | null)[][] {
  const rows = fen.split(" ")[0].split("/");
  return rows.map((row) => {
    const out: (string | null)[] = [];
    for (const ch of row) {
      if (/\d/.test(ch)) out.push(...Array(Number(ch)).fill(null));
      else out.push(ch);
    }
    return out;
  });
}

function sq(name: string): [number, number] {
  return [FILES.indexOf(name[0]), Number(name[1]) - 1];
}

export function Board({ fen, flipped, lastMove, arrow, size = 360 }: {
  fen: string;
  flipped?: boolean;
  lastMove?: string | null; // uci, highlighted squares
  arrow?: string | null; // uci, drawn as an arrow (e.g. the engine's best move)
  size?: number;
}) {
  const grid = parseFen(fen);
  const s = size / 8;
  const pos = (file: number, rank: number) => {
    const x = flipped ? 7 - file : file;
    const y = flipped ? rank : 7 - rank;
    return [x * s, y * s];
  };
  const hi = new Set<string>();
  if (lastMove) {
    hi.add(lastMove.slice(0, 2));
    hi.add(lastMove.slice(2, 4));
  }
  const squares = [];
  for (let r = 0; r < 8; r++) {
    for (let f = 0; f < 8; f++) {
      const [x, y] = pos(f, r);
      const name = FILES[f] + (r + 1);
      const dark = (f + r) % 2 === 0;
      squares.push(
        <rect key={name} x={x} y={y} width={s} height={s}
          className={`sq ${dark ? "sq-dark" : "sq-light"} ${hi.has(name) ? "sq-hi" : ""}`} />,
      );
    }
  }
  const pieces = [];
  for (let row = 0; row < 8; row++) {
    for (let f = 0; f < 8; f++) {
      const p = grid[row][f];
      if (!p) continue;
      const [x, y] = pos(f, 7 - row);
      const white = p === p.toUpperCase();
      pieces.push(
        <text key={`${row}-${f}`} x={x + s / 2} y={y + s * 0.8} textAnchor="middle" fontSize={s * 0.82}
          className={white ? "pc-white" : "pc-black"}>{GLYPH[p.toLowerCase()]}</text>,
      );
    }
  }
  let arrowEl = null;
  if (arrow && arrow.length >= 4) {
    const [f1, r1] = sq(arrow.slice(0, 2));
    const [f2, r2] = sq(arrow.slice(2, 4));
    const [x1, y1] = pos(f1, r1);
    const [x2, y2] = pos(f2, r2);
    arrowEl = (
      <line x1={x1 + s / 2} y1={y1 + s / 2} x2={x2 + s / 2} y2={y2 + s / 2} className="arrow" strokeWidth={s / 7}
        markerEnd="url(#arrowhead)" />
    );
  }
  const coords = [];
  for (let i = 0; i < 8; i++) {
    const file = flipped ? 7 - i : i;
    const rank = flipped ? i + 1 : 8 - i;
    coords.push(<text key={`f${i}`} x={i * s + s - 3} y={size - 3} textAnchor="end" className="coord" fontSize={s * 0.2}>{FILES[file]}</text>);
    coords.push(<text key={`r${i}`} x={3} y={i * s + s * 0.24} className="coord" fontSize={s * 0.2}>{rank}</text>);
  }
  return (
    <svg className="board" viewBox={`0 0 ${size} ${size}`} role="img" aria-label={`Board position ${fen}`}>
      <defs>
        <marker id="arrowhead" markerWidth="4" markerHeight="4" refX="2.2" refY="2" orient="auto">
          <path d="M0,0 L4,2 L0,4 Z" className="arrow-head" />
        </marker>
      </defs>
      {squares}
      {coords}
      {arrowEl}
      {pieces}
    </svg>
  );
}
