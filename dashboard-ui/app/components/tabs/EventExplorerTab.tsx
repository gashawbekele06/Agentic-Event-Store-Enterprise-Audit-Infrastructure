"use client";
import { useState } from "react";
import { api } from "../../api";

function fmt(v: unknown): string {
  if (v == null) return "—";
  if (typeof v === "object") return JSON.stringify(v, null, 2);
  return String(v);
}

export default function EventExplorerTab() {
  const [streamId, setStreamId] = useState("");
  const [result, setResult] = useState<{ stream_id: string; total: number; events: Record<string, unknown>[] } | null>(null);
  const [loading, setLoading] = useState(false);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [offset, setOffset] = useState(0);
  const limit = 30;

  async function search(newOffset = 0) {
    if (!streamId.trim()) return;
    setLoading(true);
    try {
      const data = await api.stream(streamId.trim(), limit, newOffset);
      setResult(data);
      setOffset(newOffset);
      setExpanded(new Set());
    } finally {
      setLoading(false);
    }
  }

  function toggle(i: number) {
    setExpanded((prev) => {
      const next = new Set(prev);
      next.has(i) ? next.delete(i) : next.add(i);
      return next;
    });
  }

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

  return (
    <div className="space-y-4">
      <h2 className="text-white text-sm font-semibold tracking-widest uppercase">Stream Event Explorer</h2>

      <div className="flex gap-2">
        <input
          value={streamId}
          onChange={(e) => setStreamId(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && search()}
          placeholder="Stream ID  e.g. loan-abc123"
          className="flex-1 bg-gray-900 border border-gray-700 rounded px-3 py-2 text-xs text-gray-300 font-mono placeholder-gray-600 focus:outline-none focus:border-indigo-500"
        />
        <button
          onClick={() => search()}
          className="px-4 py-2 bg-indigo-600 hover:bg-indigo-500 rounded text-xs text-white transition-colors"
        >
          Load Stream
        </button>
      </div>

      {result && (
        <>
          <div className="flex items-center justify-between text-xs text-gray-500">
            <span>Stream: <span className="font-mono text-indigo-300">{result.stream_id}</span> — {result.total} total events</span>
            <div className="flex gap-2">
              <button
                disabled={offset === 0}
                onClick={() => search(offset - limit)}
                className="px-3 py-1 bg-gray-800 hover:bg-gray-700 disabled:opacity-30 rounded transition-colors"
              >
                ← Prev
              </button>
              <button
                disabled={offset + limit >= result.total}
                onClick={() => search(offset + limit)}
                className="px-3 py-1 bg-gray-800 hover:bg-gray-700 disabled:opacity-30 rounded transition-colors"
              >
                Next →
              </button>
            </div>
          </div>

          {/* Timeline */}
          <div className="relative">
            <div className="absolute left-5 top-0 bottom-0 w-px bg-gray-800" />
            <div className="space-y-3">
              {result.events.map((e, i) => (
                <div key={i} className="relative pl-12">
                  <div className="absolute left-3.5 top-2 w-3 h-3 rounded-full border-2 border-indigo-600 bg-gray-900 z-10" />
                  <div className="bg-gray-900 border border-gray-800 rounded-lg p-3 hover:border-gray-700 transition-colors">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className={`text-xs px-2 py-0.5 rounded font-semibold ${TYPE_COLORS[String(e.event_type)] ?? "bg-gray-800 text-gray-300"}`}>
                        {String(e.event_type)}
                      </span>
                      <span className="text-gray-700 text-xs">v{String(e.event_version)}</span>
                      <span className="text-indigo-600 text-xs font-mono">pos:{String(e.stream_position)}</span>
                      <span className="text-gray-600 text-xs ml-auto font-mono">
                        {e.recorded_at ? new Date(String(e.recorded_at)).toLocaleString() : "—"}
                      </span>
                    </div>
                    <button
                      onClick={() => toggle(i)}
                      className="mt-2 text-xs text-gray-600 hover:text-gray-400"
                    >
                      {expanded.has(i) ? "▲ Hide payload" : "▼ Show payload"}
                    </button>
                    {expanded.has(i) && (
                      <pre className="mt-2 text-xs text-gray-400 bg-black/40 rounded p-3 overflow-x-auto whitespace-pre-wrap">
                        {fmt(e.payload)}
                      </pre>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </div>
        </>
      )}

      {loading && (
        <div className="text-center py-10 text-gray-600 text-xs animate-pulse">Loading stream…</div>
      )}

      {!result && !loading && (
        <div className="text-center py-10 text-gray-700 text-xs">Enter a stream ID to explore events.</div>
      )}
    </div>
  );
}
