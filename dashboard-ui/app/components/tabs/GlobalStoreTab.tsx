"use client";
import { useEffect, useState } from "react";
import { api } from "../../api";

const TYPE_COLORS: Record<string, string> = {
  ApplicationSubmitted: "bg-blue-900 text-blue-300",
  CreditAnalysisCompleted: "bg-cyan-900 text-cyan-300",
  FraudScreeningCompleted: "bg-purple-900 text-purple-300",
  ComplianceRulePassed: "bg-green-900 text-green-300",
  ComplianceRuleFailed: "bg-red-900 text-red-300",
  DecisionGenerated: "bg-indigo-900 text-indigo-300",
  ApplicationApproved: "bg-green-900 text-green-400",
  ApplicationDeclined: "bg-red-900 text-red-400",
  AgentContextLoaded: "bg-yellow-900 text-yellow-300",
};

export default function GlobalStoreTab() {
  const [result, setResult] = useState<{ total: number; limit: number; offset: number; events: Record<string, unknown>[] } | null>(null);
  const [typeFilter, setTypeFilter] = useState("");
  const [eventTypes, setEventTypes] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [offset, setOffset] = useState(0);
  const limit = 50;

  useEffect(() => {
    api.globalEventTypes().then(setEventTypes).catch(() => {});
  }, []);

  async function load(newOffset = 0, type = typeFilter) {
    setLoading(true);
    try {
      const data = await api.globalEvents(limit, newOffset, type || undefined);
      setResult(data);
      setOffset(newOffset);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(0, typeFilter); }, [typeFilter]);

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-white text-sm font-semibold tracking-widest uppercase">Global Event Store</h2>
        <button
          onClick={() => load(offset)}
          className="px-3 py-1.5 bg-gray-800 hover:bg-gray-700 rounded text-xs"
        >
          ↻ Refresh
        </button>
      </div>

      <div className="flex gap-2 items-center">
        <select
          value={typeFilter}
          onChange={(e) => setTypeFilter(e.target.value)}
          className="bg-gray-900 border border-gray-700 rounded px-3 py-1.5 text-xs text-gray-300 focus:outline-none focus:border-indigo-500"
        >
          <option value="">All Event Types</option>
          {eventTypes.map((t) => (
            <option key={t} value={t}>{t}</option>
          ))}
        </select>
        {result && (
          <span className="text-gray-600 text-xs ml-auto">{result.total.toLocaleString()} total events</span>
        )}
      </div>

      {result && (
        <div className="flex justify-between items-center text-xs text-gray-500">
          <span>Showing {offset + 1}–{Math.min(offset + limit, result.total)} of {result.total}</span>
          <div className="flex gap-2">
            <button
              disabled={offset === 0}
              onClick={() => load(offset - limit)}
              className="px-3 py-1 bg-gray-800 hover:bg-gray-700 disabled:opacity-30 rounded"
            >
              ← Prev
            </button>
            <button
              disabled={offset + limit >= result.total}
              onClick={() => load(offset + limit)}
              className="px-3 py-1 bg-gray-800 hover:bg-gray-700 disabled:opacity-30 rounded"
            >
              Next →
            </button>
          </div>
        </div>
      )}

      <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
        <table className="w-full text-xs">
          <thead>
            <tr className="text-gray-600 border-b border-gray-800">
              {["Pos", "Type", "Stream ID", "Ver", "Recorded At"].map((h) => (
                <th key={h} className="text-left p-3 font-normal tracking-wider">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan={5} className="py-10 text-center text-gray-600 animate-pulse">Loading events…</td></tr>
            ) : result?.events.length === 0 ? (
              <tr><td colSpan={5} className="py-10 text-center text-gray-600">No events found.</td></tr>
            ) : result?.events.map((e, i) => (
              <tr key={i} className="border-b border-gray-800/40 hover:bg-gray-800/30">
                <td className="p-3 text-indigo-600 font-mono">{String(e.global_position ?? e.stream_position ?? "—")}</td>
                <td className="p-3">
                  <span className={`px-2 py-0.5 rounded text-xs font-semibold ${TYPE_COLORS[String(e.event_type)] ?? "bg-gray-800 text-gray-300"}`}>
                    {String(e.event_type)}
                  </span>
                </td>
                <td className="p-3 font-mono text-gray-400 max-w-40 truncate">{String(e.stream_id)}</td>
                <td className="p-3 text-gray-600">v{String(e.event_version)}</td>
                <td className="p-3 text-gray-600 font-mono">
                  {e.recorded_at ? new Date(String(e.recorded_at)).toLocaleString() : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
