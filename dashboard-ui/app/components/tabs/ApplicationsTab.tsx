"use client";
import { useEffect, useState, useCallback } from "react";
import { api } from "../../api";

const STATE_COLORS: Record<string, string> = {
  SUBMITTED: "text-blue-400",
  AWAITING_ANALYSIS: "text-indigo-400",
  ANALYSIS_COMPLETE: "text-cyan-400",
  COMPLIANCE_REVIEW: "text-yellow-400",
  PENDING_DECISION: "text-purple-400",
  PENDING_HUMAN_REVIEW: "text-orange-400",
  FINAL_APPROVED: "text-green-400",
  FINAL_DECLINED: "text-red-400",
};

function fmt(v: unknown) {
  if (v == null) return "—";
  return String(v);
}
function fmtMoney(v: unknown) {
  if (v == null) return "—";
  return "$" + Number(v).toLocaleString();
}
function fmtDate(v: unknown) {
  if (!v) return "—";
  return new Date(String(v)).toLocaleString();
}

export default function ApplicationsTab() {
  const [apps, setApps] = useState<Record<string, unknown>[]>([]);
  const [loading, setLoading] = useState(true);
  const [stateFilter, setStateFilter] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<Record<string, unknown> | null>(null);
  const [events, setEvents] = useState<Record<string, unknown>[]>([]);
  const [compliance, setCompliance] = useState<Record<string, unknown>[]>([]);
  const [detailTab, setDetailTab] = useState<"events" | "compliance">("events");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api.applications(stateFilter || undefined);
      setApps(data);
    } finally {
      setLoading(false);
    }
  }, [stateFilter]);

  useEffect(() => { load(); }, [load]);

  async function openDetail(id: string) {
    setSelected(id);
    const [d, e, c] = await Promise.all([
      api.application(id),
      api.applicationEvents(id),
      api.applicationCompliance(id),
    ]);
    setDetail(d);
    setEvents(e);
    setCompliance(c);
  }

  const STATES = [
    "", "SUBMITTED", "AWAITING_ANALYSIS", "ANALYSIS_COMPLETE",
    "COMPLIANCE_REVIEW", "PENDING_DECISION", "PENDING_HUMAN_REVIEW",
    "FINAL_APPROVED", "FINAL_DECLINED",
  ];

  return (
    <div className="flex flex-col gap-4">
      {/* Toolbar */}
      <div className="flex items-center gap-3">
        <select
          value={stateFilter}
          onChange={(e) => setStateFilter(e.target.value)}
          className="bg-gray-900 border border-gray-700 rounded px-3 py-1.5 text-xs text-gray-300 focus:outline-none focus:border-indigo-500"
        >
          {STATES.map((s) => (
            <option key={s} value={s}>{s || "All States"}</option>
          ))}
        </select>
        <button
          onClick={load}
          className="px-3 py-1.5 bg-gray-800 hover:bg-gray-700 rounded text-xs transition-colors"
        >
          ↻ Refresh
        </button>
        <span className="text-gray-600 text-xs ml-auto">{apps.length} applications</span>
      </div>

      {/* Table */}
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="text-gray-600 border-b border-gray-800">
              {["Application ID","State","Applicant","Requested","Approved","Risk","Fraud","Compliance","Last Event"].map((h) => (
                <th key={h} className="text-left pb-2 font-normal tracking-wider pr-4">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan={9} className="py-10 text-center text-gray-600">Loading…</td></tr>
            ) : apps.length === 0 ? (
              <tr><td colSpan={9} className="py-10 text-center text-gray-600">No applications found.</td></tr>
            ) : apps.map((a) => (
              <tr
                key={String(a.application_id)}
                className="border-b border-gray-800/50 hover:bg-gray-900 cursor-pointer transition-colors"
                onClick={() => openDetail(String(a.application_id))}
              >
                <td className="py-2 pr-4 font-mono text-indigo-300 max-w-32 truncate">
                  {String(a.application_id).slice(0, 8)}…
                </td>
                <td className={`py-2 pr-4 font-semibold ${STATE_COLORS[String(a.state)] ?? "text-gray-400"}`}>
                  {fmt(a.state)}
                </td>
                <td className="py-2 pr-4 text-gray-300">{fmt(a.applicant_id)}</td>
                <td className="py-2 pr-4 text-gray-300">{fmtMoney(a.requested_amount_usd)}</td>
                <td className="py-2 pr-4 text-green-400">{fmtMoney(a.approved_amount_usd)}</td>
                <td className={`py-2 pr-4 ${a.risk_tier === "HIGH" ? "text-red-400" : a.risk_tier === "MEDIUM" ? "text-yellow-400" : "text-green-400"}`}>
                  {fmt(a.risk_tier)}
                </td>
                <td className={`py-2 pr-4 ${Number(a.fraud_score) > 0.6 ? "text-red-400" : "text-gray-300"}`}>
                  {a.fraud_score != null ? Number(a.fraud_score).toFixed(3) : "—"}
                </td>
                <td className={`py-2 pr-4 ${a.compliance_status === "PASSED" ? "text-green-400" : a.compliance_status === "FAILED" ? "text-red-400" : "text-gray-400"}`}>
                  {fmt(a.compliance_status)}
                </td>
                <td className="py-2 text-gray-600">{fmtDate(a.last_event_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Detail panel */}
      {selected && detail && (
        <div className="bg-gray-900 border border-gray-700 rounded-lg p-5 mt-2">
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-gray-300 font-semibold text-sm">Application Detail</h3>
            <button onClick={() => setSelected(null)} className="text-gray-600 hover:text-gray-300 text-xs">✕ Close</button>
          </div>
          <p className="font-mono text-indigo-300 text-xs mb-4">{String(detail.application_id)}</p>

          {/* Sub-tabs */}
          <div className="flex gap-2 mb-4 border-b border-gray-800 pb-2">
            {(["events", "compliance"] as const).map((t) => (
              <button
                key={t}
                onClick={() => setDetailTab(t)}
                className={`px-3 py-1 rounded text-xs uppercase tracking-wider transition-colors ${
                  detailTab === t ? "bg-indigo-600 text-white" : "text-gray-500 hover:text-gray-300"
                }`}
              >
                {t}
              </button>
            ))}
          </div>

          {detailTab === "events" && (
            <div className="space-y-2 max-h-80 overflow-y-auto">
              {events.map((e, i) => (
                <div key={i} className="flex items-start gap-3 border-b border-gray-800/50 pb-2">
                  <span className="text-gray-700 text-xs w-5 shrink-0">{String(e.stream_position)}</span>
                  <div className="flex-1">
                    <span className="text-indigo-300 text-xs font-semibold">{String(e.event_type)}</span>
                    <span className="text-gray-700 text-xs ml-2">v{String(e.event_version)}</span>
                    <p className="text-gray-600 text-xs mt-0.5">{fmtDate(e.recorded_at)}</p>
                  </div>
                </div>
              ))}
              {events.length === 0 && <p className="text-gray-600 text-xs">No events.</p>}
            </div>
          )}

          {detailTab === "compliance" && (
            <div className="space-y-2 max-h-80 overflow-y-auto">
              {compliance.map((c, i) => (
                <div key={i} className="flex items-center gap-3 border-b border-gray-800/50 pb-2">
                  <span className={`text-xs font-bold px-2 py-0.5 rounded ${
                    c.status === "PASSED" ? "bg-green-900 text-green-400" :
                    c.status === "FAILED" ? "bg-red-900 text-red-400" : "bg-gray-800 text-gray-400"
                  }`}>{String(c.status)}</span>
                  <span className="text-gray-300 text-xs font-mono">{String(c.rule_id)}</span>
                  <span className="text-gray-600 text-xs">{fmt(c.rule_version)}</span>
                  {c.failure_reason != null && <span className="text-red-400 text-xs">{String(c.failure_reason)}</span>}
                </div>
              ))}
              {compliance.length === 0 && <p className="text-gray-600 text-xs">No compliance checks.</p>}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
