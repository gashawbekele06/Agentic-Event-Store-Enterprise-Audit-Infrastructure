"use client";
import { useEffect, useState } from "react";
import {
  BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid,
  RadarChart, Radar, PolarGrid, PolarAngleAxis, PolarRadiusAxis, Legend,
} from "recharts";
import { api } from "../../api";

function num(n: unknown) { return n != null ? Number(n).toLocaleString() : "—"; }
function score(n: unknown) { return n != null ? Number(n).toFixed(3) : "—"; }
function fmtDate(v: unknown) { return v ? new Date(String(v)).toLocaleString() : "—"; }


const TOOLTIP_STYLE = {
  contentStyle: { background: "#1f2937", border: "1px solid #374151", borderRadius: 6, fontSize: 11 },
  labelStyle: { color: "#9ca3af" },
  itemStyle: { color: "#e5e7eb" },
};

export default function AgentsTab() {
  const [agents, setAgents] = useState<Record<string, unknown>[]>([]);
  const [loading, setLoading] = useState(true);

  function refresh() {
    setLoading(true);
    api.agents().then(setAgents).finally(() => setLoading(false));
  }

  useEffect(() => { refresh(); }, []);

  const maxAnalyses = Math.max(1, ...agents.map((a) => Number(a.analyses_completed)));

  // Charts data
  const barData = agents.map((a) => ({
    name: String(a.agent_id).slice(-8),
    analyses: Number(a.analyses_completed ?? 0),
    decisions: Number(a.decisions_generated ?? 0),
    overrides: Number(a.human_override_count ?? 0),
  }));

  const decisionData = agents.map((a) => ({
    name: String(a.agent_id).slice(-8),
    approve: Number(a.approve_count ?? 0),
    decline: Number(a.decline_count ?? 0),
    refer:   Number(a.refer_count ?? 0),
  }));

  const radarData = agents.slice(0, 5).map((a) => ({
    agent: String(a.agent_id).slice(-8),
    confidence: Number((Number(a.avg_confidence_score ?? 0) * 100).toFixed(1)),
    analyses:   Math.round((Number(a.analyses_completed ?? 0) / maxAnalyses) * 100),
    speed:      a.avg_duration_ms != null ? Math.max(0, 100 - Number(a.avg_duration_ms) / 10) : 0,
  }));

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-white text-sm font-semibold tracking-widest uppercase">Agent Performance Ledger</h2>
        <button onClick={refresh} className="px-3 py-1.5 bg-gray-800 hover:bg-gray-700 rounded text-xs">↻ Refresh</button>
      </div>

      {/* KPI summary */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        {[
          { label: "TOTAL AGENTS",    value: agents.length,                                                                     color: "text-white" },
          { label: "TOTAL ANALYSES",  value: num(agents.reduce((s, a) => s + Number(a.analyses_completed), 0)),  color: "text-indigo-400" },
          { label: "TOTAL DECISIONS", value: num(agents.reduce((s, a) => s + Number(a.decisions_generated), 0)), color: "text-cyan-400" },
          { label: "TOTAL OVERRIDES", value: num(agents.reduce((s, a) => s + Number(a.human_override_count), 0)), color: "text-yellow-400" },
        ].map(({ label, value, color }) => (
          <div key={label} className="bg-gray-900 border border-gray-800 rounded-lg p-4">
            <div className="text-gray-600 text-xs tracking-widest mb-1">{label}</div>
            <div className={`text-2xl font-bold ${color}`}>{value}</div>
          </div>
        ))}
      </div>

      {/* Charts row */}
      {!loading && agents.length > 0 && (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {/* Analyses vs Decisions grouped bar */}
          <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
            <h3 className="text-gray-400 text-xs font-semibold tracking-widest uppercase mb-3">
              Analyses vs Decisions
            </h3>
            <ResponsiveContainer width="100%" height={200}>
              <BarChart data={barData} margin={{ top: 4, right: 8, bottom: 0, left: -20 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
                <XAxis dataKey="name" tick={{ fill: "#4b5563", fontSize: 9 }} />
                <YAxis tick={{ fill: "#4b5563", fontSize: 10 }} />
                <Tooltip {...TOOLTIP_STYLE} />
                <Legend wrapperStyle={{ fontSize: 10, color: "#9ca3af" }} />
                <Bar dataKey="analyses"  name="Analyses"  fill="#6366f1" radius={[3,3,0,0]} />
                <Bar dataKey="decisions" name="Decisions" fill="#22d3ee" radius={[3,3,0,0]} />
                <Bar dataKey="overrides" name="Overrides" fill="#fbbf24" radius={[3,3,0,0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>

          {/* Decision breakdown stacked bar */}
          <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
            <h3 className="text-gray-400 text-xs font-semibold tracking-widest uppercase mb-3">
              Decision Breakdown
            </h3>
            <ResponsiveContainer width="100%" height={200}>
              <BarChart data={decisionData} margin={{ top: 4, right: 8, bottom: 0, left: -20 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
                <XAxis dataKey="name" tick={{ fill: "#4b5563", fontSize: 9 }} />
                <YAxis tick={{ fill: "#4b5563", fontSize: 10 }} />
                <Tooltip {...TOOLTIP_STYLE} />
                <Legend wrapperStyle={{ fontSize: 10, color: "#9ca3af" }} />
                <Bar dataKey="approve" name="Approve" fill="#34d399" stackId="d" />
                <Bar dataKey="refer"   name="Refer"   fill="#fbbf24" stackId="d" />
                <Bar dataKey="decline" name="Decline" fill="#f87171" stackId="d" radius={[3,3,0,0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>

          {/* Radar chart — relative performance */}
          {radarData.length > 1 && (
            <div className="bg-gray-900 border border-gray-800 rounded-lg p-4 md:col-span-2">
              <h3 className="text-gray-400 text-xs font-semibold tracking-widest uppercase mb-3">
                Relative Performance (top 5 agents) — Confidence · Volume · Speed
              </h3>
              <ResponsiveContainer width="100%" height={250}>
                <RadarChart data={[
                  { metric: "Confidence", ...Object.fromEntries(radarData.map(r => [r.agent, r.confidence])) },
                  { metric: "Volume",     ...Object.fromEntries(radarData.map(r => [r.agent, r.analyses])) },
                  { metric: "Speed",      ...Object.fromEntries(radarData.map(r => [r.agent, r.speed])) },
                ]}>
                  <PolarGrid stroke="#1f2937" />
                  <PolarAngleAxis dataKey="metric" tick={{ fill: "#6b7280", fontSize: 11 }} />
                  <PolarRadiusAxis domain={[0, 100]} tick={{ fill: "#4b5563", fontSize: 9 }} />
                  {radarData.map((r, i) => (
                    <Radar key={r.agent} name={r.agent} dataKey={r.agent}
                      stroke={["#6366f1","#22d3ee","#34d399","#fbbf24","#f87171"][i]}
                      fill={["#6366f1","#22d3ee","#34d399","#fbbf24","#f87171"][i]}
                      fillOpacity={0.12} />
                  ))}
                  <Legend wrapperStyle={{ fontSize: 10, color: "#9ca3af" }} />
                  <Tooltip {...TOOLTIP_STYLE} />
                </RadarChart>
              </ResponsiveContainer>
            </div>
          )}
        </div>
      )}

      {/* Agent table */}
      <div className="bg-gray-900 border border-gray-800 rounded-lg p-5">
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-gray-600 border-b border-gray-800">
                {["Agent ID","Model","Analyses","Decisions","Avg Conf.","Avg Duration","Approve","Decline","Refer","Overrides","Last Seen"].map((h) => (
                  <th key={h} className="text-left pb-2 font-normal tracking-wider pr-3">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {loading ? (
                <tr><td colSpan={11} className="py-10 text-center text-gray-600">Loading…</td></tr>
              ) : agents.length === 0 ? (
                <tr><td colSpan={11} className="py-10 text-center text-gray-600">No agent data.</td></tr>
              ) : agents.map((a, i) => (
                <tr key={i} className="border-b border-gray-800/40 hover:bg-gray-800/30">
                  <td className="py-2 pr-3 font-mono text-indigo-300 max-w-32 truncate">{String(a.agent_id)}</td>
                  <td className="py-2 pr-3 text-gray-400 font-mono">{String(a.model_version)}</td>
                  <td className="py-2 pr-3">
                    <div className="flex items-center gap-2">
                      <span className="text-white font-semibold">{num(a.analyses_completed)}</span>
                      <div className="w-12 h-1.5 bg-gray-800 rounded-full overflow-hidden">
                        <div className="h-full bg-indigo-600 rounded-full" style={{ width: `${(Number(a.analyses_completed) / maxAnalyses) * 100}%` }} />
                      </div>
                    </div>
                  </td>
                  <td className="py-2 pr-3 text-gray-300">{num(a.decisions_generated)}</td>
                  <td className="py-2 pr-3 text-cyan-400">{score(a.avg_confidence_score)}</td>
                  <td className="py-2 pr-3 text-gray-400">{a.avg_duration_ms != null ? Number(a.avg_duration_ms).toFixed(0) + "ms" : "—"}</td>
                  <td className="py-2 pr-3 text-green-400">{num(a.approve_count)}</td>
                  <td className="py-2 pr-3 text-red-400">{num(a.decline_count)}</td>
                  <td className="py-2 pr-3 text-yellow-400">{num(a.refer_count)}</td>
                  <td className={`py-2 pr-3 ${Number(a.human_override_count) > 0 ? "text-orange-400" : "text-gray-600"}`}>
                    {num(a.human_override_count)}
                  </td>
                  <td className="py-2 text-gray-600 font-mono">{fmtDate(a.last_seen_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
