import { useState } from "react";
import { DashboardView } from "./DashboardView";
import { HistoryView } from "./HistoryView";
import { ImportView } from "./ImportView";
import { QueueView } from "./QueueView";
import { ReportView } from "./ReportView";
import { SuggestView } from "./SuggestView";

type Tab = "home" | "history" | "import" | "suggest" | "queue";
const TABS: { id: Tab; label: string }[] = [
  { id: "home", label: "Home" },
  { id: "history", label: "Game history" },
  { id: "import", label: "Import" },
  { id: "suggest", label: "Suggest a set" },
  { id: "queue", label: "Analysis queue" },
];

export function App() {
  const [tab, setTab] = useState<Tab>("home");
  const [report, setReport] = useState<{ gameId: number; ply?: number; from: Tab } | null>(null);
  const [gamesVersion, setGamesVersion] = useState(0);
  const [queueVersion, setQueueVersion] = useState(0);
  const bumpGames = () => setGamesVersion((v) => v + 1);
  const bumpQueue = () => setQueueVersion((v) => v + 1);
  const open = (gameId: number, ply?: number) => {
    setReport({ gameId, ply, from: tab });
    window.scrollTo(0, 0);
  };
  const go = (t: Tab) => {
    setReport(null);
    setTab(t);
  };

  return (
    <>
      <header className="top">
        <div className="brand">
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
        <div hidden={report !== null || tab !== "import"}><ImportView onImported={bumpGames} /></div>
        {report ? (
          <ReportView gameId={report.gameId} initialPly={report.ply} onOpen={open} onBack={() => go(report.from)} />
        ) : (
          <>
            {tab === "home" && <DashboardView refreshKey={gamesVersion + queueVersion} onOpen={open} />}
            {tab === "history" && <HistoryView refreshKey={gamesVersion + queueVersion} onQueued={bumpQueue} onOpen={open} />}
            {tab === "suggest" && <SuggestView onQueued={() => { bumpQueue(); bumpGames(); }} />}
            {tab === "queue" && <QueueView refreshKey={queueVersion} />}
          </>
        )}
      </main>
    </>
  );
}
