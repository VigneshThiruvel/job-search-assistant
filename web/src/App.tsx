import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "./api/client";
import ApplicationsTab from "./tabs/ApplicationsTab";
import InboxTab from "./tabs/InboxTab";
import TodayTab from "./tabs/TodayTab";

const TABS = { today: "Today", inbox: "Inbox", applications: "Applications" } as const;
type Tab = keyof typeof TABS;

function tabFromHash(): Tab {
  const hash = window.location.hash.slice(1);
  return hash in TABS ? (hash as Tab) : "today";
}

export default function App() {
  const [tab, setTab] = useState<Tab>(tabFromHash);
  const status = useQuery({ queryKey: ["status"], queryFn: api.status });

  useEffect(() => {
    const onHash = () => setTab(tabFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const s = status.data;
  const badge: Record<Tab, number | undefined> = {
    today: s?.queued,
    inbox: s?.inbox_open,
    applications: s ? s.selected + s.ready : undefined,
  };

  return (
    <div className="app">
      <header>
        <h1>Job search</h1>
        <nav>
          {(Object.keys(TABS) as Tab[]).map((t) => (
            <a key={t} href={`#${t}`} className={t === tab ? "active" : ""}>
              {TABS[t]}
              {badge[t] ? <span className="badge">{badge[t]}</span> : null}
            </a>
          ))}
        </nav>
      </header>
      {s && (s.unembedded > 0 || s.pending_extraction > 0) && (
        <p className="hint">
          {s.unembedded > 0 && <>{s.unembedded} posting(s) need embedding: run <code>daily --skip-ingest</code>. </>}
          {s.pending_extraction > 0 && <>{s.pending_extraction} fresh posting(s) wait for the agent: run <code>/mcp__job-search__daily_run</code>.</>}
        </p>
      )}
      {status.isError && <p className="error">Backend not reachable: is the API running?</p>}
      <main>
        {tab === "today" && <TodayTab />}
        {tab === "inbox" && <InboxTab />}
        {tab === "applications" && <ApplicationsTab />}
      </main>
    </div>
  );
}
