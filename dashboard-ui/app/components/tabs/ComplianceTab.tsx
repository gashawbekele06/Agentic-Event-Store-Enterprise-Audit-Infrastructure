"use client";
import { useEffect, useState } from "react";
import { api } from "../../api";

export default function ComplianceTab() {
  const [appId, setAppId] = useState("");
  const [checks, setChecks] = useState<Record<string, unknown>[]>([]);
  const [loading, setLoading] = useState(false);
  const [searched, setSearched] = useState(false);

  async function search() {
    if (!appId.trim()) return;
    setLoading(true);
    setSearched(true);
    try {
      const data = await api.applicationCompliance(appId.trim());
      setChecks(data);
    } finally {
      setLoading(false);
    }
  }

  const passed = checks.filter((c) => c.status === "PASSED").length;
  const failed = checks.filter((c) => c.status === "FAILED").length;
  const pending = checks.filter((c) => c.status === "PENDING").length;

  return (
    <div className="space-y-4">
      <h2 className="text-white text-sm font-semibold tracking-widest uppercase">Compliance Audit View</h2>

      <div className="flex gap-2">
        <input
          value={appId}
          onChange={(e) => setAppId(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && search()}
          placeholder="Application ID…"
          className="flex-1 bg-gray-900 border border-gray-700 rounded px-3 py-2 text-xs text-gray-300 font-mono placeholder-gray-600 focus:outline-none focus:border-indigo-500"
        />
        <button
          onClick={search}
          className="px-4 py-2 bg-indigo-600 hover:bg-indigo-500 rounded text-xs text-white transition-colors"
        >
          Query
        </button>
      </div>

      {searched && !loading && (
        <>
          {/* Summary pills */}
          {checks.length > 0 && (
            <div className="flex gap-3">
              <span className="bg-green-900 text-green-400 text-xs px-3 py-1 rounded-full">{passed} PASSED</span>
              <span className="bg-red-900 text-red-400 text-xs px-3 py-1 rounded-full">{failed} FAILED</span>
              <span className="bg-gray-800 text-gray-400 text-xs px-3 py-1 rounded-full">{pending} PENDING</span>
            </div>
          )}

          <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-gray-600 border-b border-gray-800">
                  {["Status","Rule ID","Version","Regulation Set","Evidence Hash","Evaluated At","Remediation"].map((h) => (
                    <th key={h} className="text-left p-3 font-normal tracking-wider">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {checks.length === 0 ? (
                  <tr><td colSpan={7} className="py-10 text-center text-gray-600">No compliance records for this application.</td></tr>
                ) : checks.map((c, i) => (
                  <tr key={i} className="border-b border-gray-800/40 hover:bg-gray-800/30">
                    <td className="p-3">
                      <span className={`px-2 py-0.5 rounded text-xs font-bold ${
                        c.status === "PASSED" ? "bg-green-900 text-green-400" :
                        c.status === "FAILED" ? "bg-red-900 text-red-400" : "bg-gray-800 text-gray-400"
                      }`}>{String(c.status)}</span>
                    </td>
                    <td className="p-3 font-mono text-gray-300">{String(c.rule_id)}</td>
                    <td className="p-3 text-gray-500">{String(c.rule_version ?? "—")}</td>
                    <td className="p-3 text-gray-500">{String(c.regulation_set_version ?? "—")}</td>
                    <td className="p-3 font-mono text-gray-600 text-xs max-w-32 truncate">{String(c.evidence_hash ?? "—")}</td>
                    <td className="p-3 text-gray-600">
                      {c.evaluation_timestamp ? new Date(String(c.evaluation_timestamp)).toLocaleString() : "—"}
                    </td>
                    <td className="p-3">
                      {c.remediation_required === true
                        ? <span className="text-orange-400">Required</span>
                        : <span className="text-gray-600">—</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      {loading && (
        <div className="text-center py-10 text-gray-600 text-xs animate-pulse">Loading compliance records…</div>
      )}

      {!searched && (
        <div className="text-center py-10 text-gray-700 text-xs">Enter an application ID to query compliance records.</div>
      )}
    </div>
  );
}
