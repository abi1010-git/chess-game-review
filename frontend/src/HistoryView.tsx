import type { Tab } from "./App";
import { useEffect, useMemo, useState } from "react";
import { api, type Facets, type Filters, type Game, type Preview } from "./api";
import { GameRow, GameTableHead, PROVIDER_LABEL } from "./format";

const PAGE = 50;

export function HistoryView({ refreshKey, onQueued, onOpen, onGo }: { refreshKey: number; onQueued: () => void; onOpen: (id: number) => void; onGo: (t: Tab) => void }) {
  const [facets, setFacets] = useState<Facets | null>(null);
  const [filters, setFilters] = useState<Filters>({});
  const [page, setPage] = useState(0);
  const [data, setData] = useState<{ total: number; items: Game[] } | null>(null);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [preview, setPreview] = useState<Preview | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    api.facets().then(setFacets).catch((e) => setError(e.message));
  }, [refreshKey]);

  useEffect(() => {
    api.games(filters, PAGE, page * PAGE).then(setData).catch((e) => setError(e.message));
  }, [filters, page, refreshKey, reloadKey]);

  const selectedIds = useMemo(() => [...selected], [selected]);
  useEffect(() => {
    if (selectedIds.length === 0) return setPreview(null);
    const t = setTimeout(() => api.preview(selectedIds).then(setPreview).catch(() => setPreview(null)), 200);
    return () => clearTimeout(t);
  }, [selectedIds, reloadKey]);

  function set<K extends keyof Filters>(k: K, v: Filters[K]) {
    setPage(0);
    setFilters((f) => ({ ...f, [k]: v }));
  }

  function toggleList(k: "time_class" | "provider" | "result", v: string) {
    const cur = filters[k] ?? [];
    set(k, cur.includes(v) ? cur.filter((x) => x !== v) : [...cur, v]);
  }

  function toggle(id: number) {
    setSelected((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });
  }

  const pageIds = data?.items.map((g) => g.id) ?? [];
  const pageAllChecked = pageIds.length > 0 && pageIds.every((id) => selected.has(id));
  function togglePage() {
    setSelected((s) => {
      const n = new Set(s);
      pageIds.forEach((id) => (pageAllChecked ? n.delete(id) : n.add(id)));
      return n;
    });
  }

  async function selectAllFiltered() {
    const { ids, truncated } = await api.gameIds(filters);
    setSelected((s) => new Set([...s, ...ids]));
    if (truncated) setMessage(`Selected the first ${ids.length} games; a batch is limited to ${ids.length}.`);
  }

  async function queue() {
    setBusy(true);
    setError(null);
    try {
      const b = await api.createBatch({ game_ids: selectedIds, mode: "manual", rationale: { filters } });
      const s = b.summary!;
      setMessage(`Queued ${s.queued + s.requeued} game(s) for analysis` +
        (s.already_queued || s.already_analyzed ? ` (${s.already_queued} already queued, ${s.already_analyzed} already analyzed).` : "."));
      setSelected(new Set());
      setReloadKey((k) => k + 1);
      onQueued();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  if (facets && facets.total === 0) {
    return (
      <section className="card empty">
        <h2>No games yet</h2>
        <p>Import games from Lichess, Chess.com or a PGN file to see your history here.</p>
        <button className="primary" onClick={() => onGo("import")}>Import games</button>
      </section>
    );
  }

  const pages = data ? Math.ceil(data.total / PAGE) : 0;

  return (
    <div className="stack">
      <section className="card filters">
        <div className="filter-row">
          <label>From<input type="date" value={filters.date_from ?? ""} min={facets?.date_min ?? undefined} onChange={(e) => set("date_from", e.target.value)} /></label>
          <label>To<input type="date" value={filters.date_to ?? ""} max={facets?.date_max ?? undefined} onChange={(e) => set("date_to", e.target.value)} /></label>
          <label>Your color
            <select value={filters.color ?? ""} onChange={(e) => set("color", e.target.value)}>
              <option value="">Either</option><option value="white">White</option><option value="black">Black</option>
            </select>
          </label>
          <label>Rating from<input type="number" placeholder={facets?.rating_min?.toString()} value={filters.rating_min ?? ""} onChange={(e) => set("rating_min", e.target.value)} /></label>
          <label>Rating to<input type="number" placeholder={facets?.rating_max?.toString()} value={filters.rating_max ?? ""} onChange={(e) => set("rating_max", e.target.value)} /></label>
          <label>Analysis
            <select value={filters.analysis ?? ""} onChange={(e) => set("analysis", e.target.value)}>
              <option value="">Any</option><option value="not_analyzed">Not analyzed</option>
              <option value="queued">Queued</option><option value="analyzed">Analyzed</option>
            </select>
          </label>
        </div>
        <div className="filter-row chips">
          {facets?.time_classes.map((tc) => (
            <button key={tc} className={`chip ${filters.time_class?.includes(tc) ? "on" : ""}`} onClick={() => toggleList("time_class", tc)}>{tc}</button>
          ))}
          <span className="sep" />
          {facets?.providers.map((p) => (
            <button key={p} className={`chip ${filters.provider?.includes(p) ? "on" : ""}`} onClick={() => toggleList("provider", p)}>{PROVIDER_LABEL[p] ?? p}</button>
          ))}
          <span className="sep" />
          {(["win", "draw", "loss"] as const).map((r) => (
            <button key={r} className={`chip ${filters.result?.includes(r) ? "on" : ""}`} onClick={() => toggleList("result", r)}>{r}</button>
          ))}
          {Object.values(filters).some((v) => (Array.isArray(v) ? v.length : v)) && (
            <button className="link" onClick={() => { setFilters({}); setPage(0); }}>Clear filters</button>
          )}
        </div>
      </section>

      <section className="card">
        <div className="table-head">
          <h2>{data ? `${data.total} game${data.total === 1 ? "" : "s"}` : "Games"}</h2>
          {data && data.total > 0 && <button className="link" onClick={selectAllFiltered}>Select all {data.total} filtered games</button>}
          {selected.size > 0 && <button className="link" onClick={() => setSelected(new Set())}>Clear selection</button>}
        </div>
        <div className="table-wrap">
          <table>
            <GameTableHead selectable allChecked={pageAllChecked} onToggleAll={togglePage} />
            <tbody>
              {data?.items.map((g) => <GameRow key={g.id} g={g} checked={selected.has(g.id)} onToggle={() => toggle(g.id)} onOpen={onOpen} />)}
            </tbody>
          </table>
        </div>
        {pages > 1 && (
          <div className="pager">
            <button disabled={page === 0} onClick={() => setPage(page - 1)}>Previous</button>
            <span>Page {page + 1} of {pages}</span>
            <button disabled={page + 1 >= pages} onClick={() => setPage(page + 1)}>Next</button>
          </div>
        )}
      </section>

      {(selected.size > 0 || message || error) && (
        <div className="action-bar">
          {selected.size > 0 ? (
            <>
              <span><b>{selected.size}</b> selected</span>
              {preview && (
                <span className="muted">
                  {preview.will_queue} will be queued
                  {preview.already_queued ? ` · ${preview.already_queued} already queued` : ""}
                  {preview.already_analyzed ? ` · ${preview.already_analyzed} already analyzed` : ""}
                </span>
              )}
              <button className="primary" disabled={busy || !preview || preview.will_queue === 0} onClick={queue}>
                Queue analysis
              </button>
            </>
          ) : (
            <span className={error ? "error" : ""}>{error ?? message}</span>
          )}
          {selected.size === 0 && <button className="link" onClick={() => { setMessage(null); setError(null); }}>Dismiss</button>}
        </div>
      )}
    </div>
  );
}
