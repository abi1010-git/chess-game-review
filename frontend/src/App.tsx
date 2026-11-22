import { useEffect, useState } from "react";
import { api2 } from "./api";
import { DashboardView } from "./DashboardView";
import { HistoryView } from "./HistoryView";
import { ImportView } from "./ImportView";
import { QueueView } from "./QueueView";
import { ReportView } from "./ReportView";
import { SuggestView } from "./SuggestView";

export type Tab = "home" | "history" | "import" | "suggest" | "queue";
const TABS: { id: Tab; label: string }[] = [
  { id: "import", label: "1. Import" },
  { id: "history", label: "2. Pick games" },
  { id: "queue", label: "3. Analysis" },
  { id: "home", label: "Your progress" },
  { id: "suggest", label: "Suggest a set" },
];

/** The tab named in the address bar (#import, #history, ...), if any. */
function tabFromHash(): Tab | null {
  const h = window.location.hash.replace("#", "");
  return TABS.some((t) => t.id === h) ? (h as Tab) : null;
}

export function App() {
  // null until we know where to start: a new visitor with no games lands on Import.
  const [tab, setTab] = useState<Tab | null>(tabFromHash);
  useEffect(() => {
    if (tab) return;
    api2.dashboard()
      .then((d) => setTab(d.coverage.games === 0 ? "import" : d.coverage.analyzed === 0 ? "history" : "home"))
      .catch(() => setTab("import"));
  }, []);
  useEffect(() => {
    if (tab && window.location.hash !== `#${tab}`) history.replaceState(null, "", `#${tab}`);
  }, [tab]);
  useEffect(() => {
    const onHash = () => { const t = tabFromHash(); if (t) { setReport(null); setTab(t); } };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  const [report, setReport] = useState<{ gameId: number; ply?: number; from: Tab } | null>(null);
  const [gamesVersion, setGamesVersion] = useState(0);
  const [queueVersion, setQueueVersion] = useState(0);
  const bumpGames = () => setGamesVersion((v) => v + 1);
  const bumpQueue = () => setQueueVersion((v) => v + 1);
  const open = (gameId: number, ply?: number) => {
    setReport({ gameId, ply, from: tab ?? "home" });
    window.scrollTo(0, 0);
  };
  const go = (t: Tab) => {
    setReport(null);
    setTab(t);
  };

  return (
    <>
      <header className="top">
        <div className="brand" role="button" tabIndex={0} onClick={() => go("home")} onKeyDown={(e) => e.key === "Enter" && go("home")}>
          <span className="logo" aria-hidden>♞</span>
          <span>Chess Game Review</span>
        </div>
        <nav>
          {TABS.map((t) => (
            <button key={t.id} className={!report && tab === t.id ? "on" : ""} onClick={() => go(t.id)}>{t.label}</button>
          ))}
        </nav>
      </header>
      <main>
        {/* Keep the import view mounted so its progress polling continues on other tabs. */}
        <div hidden={report !== null || tab !== "import"}><ImportView onImported={bumpGames} onNext={() => go("history")} /></div>
        {tab === null && !report ? <p className="muted">Loading…</p> : report ? (
          <ReportView gameId={report.gameId} initialPly={report.ply} onOpen={open} onBack={() => go(report.from)} />
        ) : (
          <>
            {tab === "home" && <DashboardView refreshKey={gamesVersion + queueVersion} onOpen={open} onGo={go} />}
            {tab === "history" && <HistoryView refreshKey={gamesVersion + queueVersion} onQueued={() => { bumpQueue(); go("queue"); }} onOpen={open} onGo={go} />}
            {tab === "suggest" && <SuggestView onQueued={() => { bumpQueue(); bumpGames(); go("queue"); }} />}
            {tab === "queue" && <QueueView refreshKey={queueVersion} onGo={go} />}
          </>
        )}
      </main>
    </>
  );
}
