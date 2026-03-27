"use client";
import { useEffect, useState, useCallback } from "react";
import { api } from "./api";
import TabBar from "./components/TabBar";
import StatsBar from "./components/StatsBar";
import ApplicationsTab from "./components/tabs/ApplicationsTab";
import ComplianceTab from "./components/tabs/ComplianceTab";
import AgentsTab from "./components/tabs/AgentsTab";
import EventExplorerTab from "./components/tabs/EventExplorerTab";
import GlobalStoreTab from "./components/tabs/GlobalStoreTab";
import PipelineTab from "./components/tabs/PipelineTab";
import DemoSuiteTab from "./components/tabs/DemoSuiteTab";
import AnalysisTab from "./components/tabs/AnalysisTab";
import type { TabId } from "./types";

export default function Home() {
  const [tab, setTab] = useState<TabId>("home");
  const [stats, setStats] = useState<Record<string, number>>({});
  const [lags, setLags] = useState<Record<string, number>>({});
  const [health, setHealth] = useState<Record<string, unknown>>({});

  const loadMeta = useCallback(async () => {
    try {
      const [s, l, h] = await Promise.all([
        api.stats(),
        api.lag(),
        api.health(),
      ]);
      setStats(s);
      setLags(l);
      setHealth(h);
    } catch {
      // non-fatal — stats bar will show zeros
    }
  }, []);

  useEffect(() => {
    loadMeta();
    const id = setInterval(loadMeta, 15_000);
    return () => clearInterval(id);
  }, [loadMeta]);

  return (
    <main className="min-h-screen bg-gray-950 text-gray-300">
      {/* Header */}
      <header className="border-b border-gray-800 px-6 py-4 flex items-center gap-4">
        <div>
          <h1 className="text-white font-bold text-base tracking-tight">
            APEX <span className="text-indigo-400">Event Store</span>
          </h1>
          <p className="text-gray-600 text-xs tracking-widest uppercase">
            Enterprise Audit Infrastructure · Agentic Loan Pipeline
          </p>
        </div>
        <button
          onClick={loadMeta}
          className="ml-auto px-3 py-1.5 bg-gray-800 hover:bg-gray-700 rounded text-xs text-gray-400 transition-colors"
        >
          ↻ Refresh Stats
        </button>
      </header>

      <div className="px-6 pt-5">
        <StatsBar stats={stats} lags={lags} health={health} />
        <TabBar active={tab} onChange={setTab} />

        <div className="mt-5 pb-10">
          {tab === "home"         && <AnalysisTab />}
          {tab === "applications" && <ApplicationsTab />}
          {tab === "compliance"   && <ComplianceTab />}
          {tab === "agents"       && <AgentsTab />}
          {tab === "events"       && <EventExplorerTab />}
          {tab === "global"       && <GlobalStoreTab />}
          {tab === "pipeline"     && <PipelineTab />}
          {tab === "demo"         && <DemoSuiteTab />}
        </div>
      </div>
    </main>
  );
}
