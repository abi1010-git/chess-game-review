import { useEffect, useState } from "react";
import { api2 } from "./api";
import { DashboardView } from "./DashboardView";
import { HistoryView } from "./HistoryView";
import { ImportView } from "./ImportView";
import { QueueView } from "./QueueView";
import { ReportView } from "./ReportView";
import { StudyView } from "./StudyView";
import { SpaceBar } from "./SpaceBar";
import { SuggestView } from "./SuggestView";
import { coach } from "./api";

export type Tab = "home" | "history" | "import" | "suggest" | "queue";
const TABS: { id: Tab; label: string }[] = [
  { id: "import", label: "1. Import" },
  { id: "history", label: "2. Pick games" },
  { id: "queue", label: "3. Analysis" },
  { id: "home", label: "Your progress" },
  { id: "suggest", label: "Suggest by period" },
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
    const onHash = () => { const t = tabFromHash(); if (t) { setReport(null); setStudy(null); setTab(t); } };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  const [report, setReport] = useState<{ gameId: number; ply?: number; from: Tab } | null>(null);
  const [study, setStudy] = useState<number | null>(null);
  const [studyError, setStudyError] = useState<string | null>(null);
  const [gamesVersion, setGamesVersion] = useState(0);
  const [queueVersion, setQueueVersion] = useState(0);
  const bumpGames = () => setGamesVersion((v) => v + 1);
  const bumpQueue = () => setQueueVersion((v) => v + 1);
  const open = (gameId: number, ply?: number) => {
    setStudy(null);
    setReport({ gameId, ply, from: tab ?? "home" });
    window.scrollTo(0, 0);
  };
  const go = (t: Tab) => {
    setReport(null);
    setStudy(null);
    setTab(t);
  };
  const resume = (id: number) => {
    setReport(null);
    setStudy(id);
    window.scrollTo(0, 0);
  };
  const startStudy = (gameId: number, theme: string | null) => {
    setStudyError(null);
    coach.createStudy(gameId, theme).then((s) => resume(s.id)).catch((e) => setStudyError(e.message));
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
            <button key={t.id} className={!report && !study && tab === t.id ? "on" : ""} onClick={() => go(t.id)}>{t.label}</button>
          ))}
        </nav>
      </header>
      <SpaceBar version={gamesVersion} onChanged={() => { setReport(null); setStudy(null); setTab(tabFromHash() ?? "home"); bumpGames(); bumpQueue(); }} />
      <main>
        {/* Keep the import view mounted so its progress polling continues on other tabs. */}
        {studyError && <p className="notice notice-error">{studyError}</p>}
        <div hidden={report !== null || study !== null || tab !== "import"}><ImportView onImported={bumpGames} onNext={() => go("history")} /></div>
        {tab === null && !report ? <p className="muted">Loading…</p> : study !== null ? (
          <StudyView studyId={study} onBack={() => go(tab ?? "home")} onOpenReport={(id) => open(id)} />
        ) : report ? (
          <ReportView gameId={report.gameId} initialPly={report.ply} onOpen={open} onBack={() => go(report.from)} onStudy={startStudy} />
        ) : (
          <>
            {tab === "home" && <DashboardView refreshKey={gamesVersion + queueVersion} onOpen={open} onGo={go} onStudy={startStudy} onResume={resume} />}
            {tab === "history" && <HistoryView refreshKey={gamesVersion + queueVersion} onQueued={() => { bumpQueue(); go("queue"); }} onOpen={open} onGo={go} />}
            {tab === "suggest" && <SuggestView onQueued={() => { bumpQueue(); bumpGames(); go("queue"); }} />}
            {tab === "queue" && <QueueView refreshKey={queueVersion} onGo={go} />}
          </>
        )}
      </main>
    </>
  );
}
