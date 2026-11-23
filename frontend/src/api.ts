// Thin typed client for the FastAPI backend (proxied at /api in development).

export type Notice = { level: "info" | "warning" | "error"; message: string };
export type Skip = { index: number | null; label: string; reason: string };

export type ImportRun = {
  id: number;
  source: "lichess" | "chesscom" | "pgn";
  label: string;
  status: "queued" | "running" | "completed" | "completed_with_issues" | "partial" | "failed";
  params: Record<string, unknown>;
  seen: number;
  imported: number;
  duplicates: number;
  skipped: number;
  skips: Skip[];
  notices: Notice[];
  created_at: string;
  finished_at: string | null;
};

export type Game = {
  id: number;
  source_kind: "account" | "upload";
  provider: string;
  provider_game_id: string | null;
  source_url: string | null;
  white: string | null;
  black: string | null;
  result: string;
  player_color: "white" | "black";
  player_name: string;
  player_rating: number | null;
  opponent_name: string | null;
  opponent_rating: number | null;
  player_result: "win" | "loss" | "draw" | "unknown";
  played_at: string | null;
  date_raw: string | null;
  time_control: string | null;
  time_class: string;
  rated: boolean | null;
  ply_count: number;
  opening: string | null;
  eco: string | null;
  analysis_status: string;
  outdated_version?: string | null;
};

export type Facets = {
  total: number;
  date_min: string | null;
  date_max: string | null;
  rating_min: number | null;
  rating_max: number | null;
  time_classes: string[];
  providers: string[];
};

export type Filters = {
  date_from?: string;
  date_to?: string;
  time_class?: string[];
  provider?: string[];
  color?: string;
  rating_min?: string;
  rating_max?: string;
  rated?: string;
  result?: string[];
  analysis?: string;
};

export type Preview = {
  requested: number;
  found: number;
  already_analyzed: number;
  already_queued: number;
  will_queue: number;
  analysis_version: string;
};

export type SuggestGroup = {
  key: string;
  role: "target" | "comparison";
  pool: string;
  title: string;
  reason: string;
  game_ids: number[];
};

export type Suggestion = { groups: SuggestGroup[]; warnings: string[]; preview: Preview | null; advice?: { pool: string; level: "good" | "thin" | "weak"; text: string }[] };

export type Batch = {
  id: number;
  mode: "manual" | "suggested";
  analysis_version: string;
  rationale: Record<string, unknown>;
  created_at: string;
  progress: { total: number; counts: Record<string, number>; games: { game_id: number; role: string; status: string }[] };
  summary: { queued: number; requeued: number; already_queued: number; already_analyzed: number } | null;
};

export type NameInspection = {
  games_found: number;
  invalid_games: number;
  names: { name: string; games: number }[];
  suggested_player: string | null;
  needs_choice: boolean;
};

export class ApiError extends Error {}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`/api${path}`, init);
  } catch {
    throw new ApiError("Could not reach the server. Is the API running?");
  }
  if (!res.ok) {
    let message = `Request failed (${res.status})`;
    try {
      const body = await res.json();
      if (typeof body.detail === "string") message = body.detail;
      else if (Array.isArray(body.detail)) message = body.detail.map((d: { msg: string }) => d.msg).join("; ");
    } catch {
      /* not JSON */
    }
    throw new ApiError(message);
  }
  return res.json() as Promise<T>;
}

const json = (body: unknown): RequestInit => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

export function filterQuery(f: Filters): string {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(f)) {
    if (Array.isArray(v)) v.forEach((x) => p.append(k, x));
    else if (v !== undefined && v !== "") p.set(k, v);
  }
  return p.toString();
}

export const api = {
  importAccount: (provider: "lichess" | "chesscom", body: { username: string; since?: string; until?: string; max_games: number }) =>
    request<ImportRun>(`/imports/account/${provider}`, json(body)),
  inspectPgn: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return request<NameInspection>("/imports/pgn/inspect", { method: "POST", body: fd });
  },
  importPgn: (file: File, names: string[]) => {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("player_names", names.join(","));
    return request<ImportRun>("/imports/pgn", { method: "POST", body: fd });
  },
  imports: () => request<ImportRun[]>("/imports"),
  games: (f: Filters, limit: number, offset: number) =>
    request<{ total: number; items: Game[] }>(`/games?${filterQuery(f)}&limit=${limit}&offset=${offset}`),
  gameIds: (f: Filters) => request<{ ids: number[]; truncated: boolean }>(`/games/ids?${filterQuery(f)}`),
  gamesByIds: (ids: number[]) => request<Game[]>("/games/by-ids", json({ game_ids: ids })),
  facets: () => request<Facets>("/games/facets"),
  preview: (ids: number[]) => request<Preview>("/analysis/preview", json({ game_ids: ids })),
  suggest: (body: { target_from: string; target_to: string; rating_band: number; max_comparison: number }) =>
    request<Suggestion>("/selection/suggest", json(body)),
  createBatch: (body: { game_ids: number[]; mode: "manual" | "suggested"; rationale?: unknown; roles?: Record<number, string> }) =>
    request<Batch>("/analysis/batches", json(body)),
  batches: () => request<Batch[]>("/analysis/batches"),
  cancelBatch: (id: number) => request<{ cancelled: number }>(`/analysis/batches/${id}/cancel`, { method: "POST" }),
};

// ---------------------------------------------------------------- phase 2: analysis, reports, dashboard

export type Phase = "opening" | "middlegame" | "endgame";

export type WorkerStatus = {
  worker_available: boolean;
  workers: number;
  engine: string | null;
  worker_error: string | null;
};

export type QueueSummary = WorkerStatus & { analysis_version: string; counts: Record<string, number> };

export type Candidate = { uci: string; san: string; cp: number | null; mate: number | null; win: number; line: string[] };

export type Ply = {
  ply: number;
  move_number: number;
  side: "white" | "black";
  is_player: boolean;
  san: string;
  uci: string;
  fen_before: string;
  best_san: string | null;
  best_uci: string | null;
  best_cp: number | null;
  best_mate: number | null;
  played_cp: number | null;
  played_mate: number | null;
  win_best: number;
  win_played: number;
  win_drop: number;
  classification: "best" | "good" | "inaccuracy" | "mistake" | "blunder" | "forced";
  phase: Phase;
  low_info_reason: string | null;
  deep_checked: boolean;
  stable: boolean;
  candidates: Candidate[];
  reply_line: string[];
  themes: string[];
};

export type Finding = {
  id: number;
  ply: number;
  phase: Phase;
  theme: string;
  kind: "tactical" | "positional" | "strategic";
  polarity: "worse" | "better";
  confidence: "clear" | "likely" | "uncertain";
  title: string;
  summary: string;
  evidence: Record<string, unknown> & { best_line?: string[]; reply_line?: string[]; win_drop?: number };
};

export type Example = {
  game_id: number;
  ply: number;
  move_number: number;
  side: string;
  move: string;
  best_move: string | null;
  win_drop: number;
  classification: string;
  match: "exact" | "structure" | "theme";
  played_at: string | null;
  result: string;
  time_class: string;
  provider: string;
};

export type Comparison = {
  theme: string | null;
  theme_label: string | null;
  verdict: "better" | "worse" | "similar" | "mixed" | "insufficient";
  text: string;
  stats: {
    examples: number;
    games: number;
    match_quality: "exact" | "structure" | "theme" | "none";
    by_match: { exact: number; structure: number; theme: number };
    avg_drop: number | null;
    mistake_rate: number | null;
    good_rate: number | null;
  };
  change: { direction: "steady" | "improving" | "slipping"; earlier_mistake_rate: number; later_mistake_rate: number; earlier_n: number; later_n: number } | null;
  examples: Example[];
};

export type Recurrence = {
  theme: string;
  status: "recurring" | "occasional" | "not_seen" | "no_data";
  text: string;
  games: number;
  games_with_mistake: number;
  decisions: number;
  mistake_rate: number;
  by_result: Record<string, number>;
};

export type Decision = {
  ply: number;
  move_number: number;
  side: string;
  move: string;
  phase: Phase;
  classification: string;
  win_drop: number;
  finding_ids: number[];
  comparison: Comparison | null;
  recurrence: Recurrence | null;
};

export type LossClaim = {
  role: "decisive" | "earlier" | "missed";
  ply: number;
  move_number: number;
  move: string;
  best_move: string | null;
  win_before: number;
  win_after: number;
  win_drop: number;
  stable: boolean;
  basis: { kind: "finding" | "features" | "line"; summary: string; title?: string; finding_id?: number };
  recurrence?: Recurrence;
};

export type PhaseOverview = { moves: number; decisions: number; avg_drop: number | null; mistakes: number; findings: number; headline: string };

export type Report = {
  run: { analysis_version: string; engine: string; settings: Record<string, number | string>; created_at: string; summary: { accuracy: number | null; meaningful_decisions: number; low_info: number; deep_checks: number; unstable: number; classifications: Record<string, number> } };
  plies: Ply[];
  final_fen: string;
  findings: Finding[];
  decisions: Decision[];
  verdict: { kind: "full" | "short" | "no_decisions"; text: string | null; strongest: { ply: number; title: string; summary: string; confidence: string } | null };
  phases: Record<Phase, PhaseOverview>;
  loss: { text: string; decisive: LossClaim | null; earlier: LossClaim | null; missed: LossClaim | null } | null;
};

export type GameReport = WorkerStatus & { game: Game & { initial_fen: string; moves_uci: string }; report: Report | null };

export type ThemeItem = {
  theme: string;
  label: string;
  polarity: "worse" | "better";
  status: "recurring" | "emerging" | "single" | "strength";
  occurrences: number;
  games: number;
  uncertain: number;
  by_result: Record<string, number>;
  phase: Phase;
  situation_label: string | null;
  opportunities: { decisions: number; games: number; mistake_rate: number } | null;
  examples: { game_id: number; ply: number; title: string; confidence: string }[];
};

export type Dashboard = WorkerStatus & {
  analysis_version: string;
  coverage: { outdated: number; games: number; analyzed: number; queued: number; running: number; failed: number };
  recent: {
    game_id: number; played_at: string | null; date_raw: string | null; opponent: string | null; opponent_rating: number | null;
    player_rating: number | null; color: string; result: Game["player_result"]; time_class: string; provider: string;
    opening: string | null; accuracy: number | null; mistakes: number; decisions: number | null; short: boolean;
    top_finding: { title: string; ply: number; confidence: string; polarity: string } | null;
  }[];
  trends: { month: string; games: number; decisions: number; accuracy: number | null; mistakes_per_game: number; mistake_rate: number | null; avg_drop_by_phase: Record<Phase, number | null>; results: Record<string, number>; thin: boolean }[];
  phases: Record<Phase, { decisions: number; games: number; avg_drop: number | null; mistake_rate: number | null }>;
  recurring: ThemeItem[];
  strengths: ThemeItem[];
  uncertain_findings: number;
};

export const api2 = {
  dashboard: () => request<Dashboard>("/dashboard"),
  report: (gameId: number) => request<GameReport>(`/games/${gameId}/report`),
  queue: () => request<QueueSummary>("/analysis/queue"),
  queueGames: (ids: number[]) => request<Batch>("/analysis/batches", json({ game_ids: ids, mode: "manual" })),
};

// ---------------------------------------------------------------- phase 3: coach reviews and guided study

export type Ref = { game_id: number; ply: number };

export type CoachClaim = {
  ply: number;
  verdict: "better" | "worse" | "similar" | "mixed" | "insufficient" | "narrowed";
  headline: string;
  text: string;
  refs: Ref[];
  match_quality: "exact" | "structure" | "theme" | "phase" | "none";
  searches: number;
  source: "rules" | "model";
};

export type TraceStep = { actor: string; action: string; detail: Record<string, unknown> };

export type CoachReview = {
  id: number;
  game_id: number;
  mode: "rules" | "model";
  provider: string | null;
  created_at: string;
  summary: string;
  claims: CoachClaim[];
  recommended_study: { game_id: number; theme: string; theme_label: string; reason: string; from_ply: number } | null;
  handoffs: number;
  tool_calls: number;
  fallbacks: string[];
  trace: TraceStep[];
};

export type StudyPick = {
  game_id: number;
  theme: string | null;
  theme_label: string;
  reason: string;
  opponent: string | null;
  result: Game["player_result"];
  played_at: string | null;
  time_class: string;
  color: "white" | "black";
  start_move: number;
};

export type SavedStudy = { id: number; game_id: number; title: string; theme: string | null; checkpoints: number; revealed: number; notes: number; updated_at: string };

export type StudyNotes = { plans?: string; move?: string | null; reply?: string | null; text?: string };

export type StudyCheckpoint = {
  index: number;
  ply: number;
  move_number: number;
  side: "white" | "black";
  fen: string;
  prompt: string;
  notes: StudyNotes;
  revealed: boolean;
  answer?: {
    played: string; best: string | null; best_line: string[]; candidates: { san: string; win: number }[];
    win_before: number; win_after: number; verdict: string; explanation: string;
  };
  feedback?: { text: string; source: "rules" | "model"; provider: string | null } | null;
};

export type Study = {
  id: number;
  game_id: number;
  title: string;
  rationale: string;
  objective: string;
  theme: string | null;
  start_ply: number;
  mode: "rules" | "model";
  provider: string | null;
  game: { opponent: string | null; opponent_rating: number | null; player_rating: number | null; color: "white" | "black"; result: Game["player_result"]; played_at: string | null; time_class: string; opening: string | null; provider: string };
  positions: string[];
  moves: string[];
  checkpoints: StudyCheckpoint[];
};

export type CoachStatus = {
  enabled: boolean;
  providers: { name: string; label: string; model: string; available: boolean; cooling_down: boolean; used_today: number; daily_budget: number }[];
  visitor_runs_left_this_hour: number;
};

export type SampleAdvice = { pool: string; level: "good" | "thin" | "weak"; text: string; targets: number; comparisons: number };

const put = (body: unknown): RequestInit => ({ method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

export const coach = {
  status: () => request<CoachStatus>("/coach/status"),
  review: (gameId: number) => request<{ review: CoachReview | null }>(`/games/${gameId}/coach-review`),
  runReview: (gameId: number) => request<{ review: CoachReview }>(`/games/${gameId}/coach-review`, { method: "POST" }),
  studies: () => request<{ picks: StudyPick[]; saved: SavedStudy[] }>("/studies"),
  createStudy: (gameId: number, theme: string | null) => request<Study>("/studies", json({ game_id: gameId, theme })),
  study: (id: number) => request<Study>(`/studies/${id}`),
  saveNotes: (id: number, index: number, notes: StudyNotes) => request<Study>(`/studies/${id}/checkpoints/${index}`, put(notes)),
  reveal: (id: number, index: number) => request<Study>(`/studies/${id}/checkpoints/${index}/reveal`, { method: "POST" }),
  deleteStudy: (id: number) => fetch(`/api/studies/${id}`, { method: "DELETE" }),
};

export type Me = {
  mode: "single" | "guest"; has_space: boolean; games: number; sample_available: boolean; retention_days: number | null;
  limits: { max_games: number; max_file_mb: number; max_queued: number };
};
export const me = {
  get: () => request<Me>("/me"),
  loadSample: () => request<{ games: number }>("/me/sample", { method: "POST" }),
  remove: () => request<{ deleted: { games: number; studies: number } }>("/me", { method: "DELETE" }),
};
