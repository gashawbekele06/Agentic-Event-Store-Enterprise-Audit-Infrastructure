"use client";
import { useState, useEffect, useRef } from "react";
import { api } from "../../api";

const COMPANIES = Array.from({ length: 80 }, (_, i) => `COMP-${String(i + 1).padStart(3, "0")}`);

const AGENTS = [
  { key: "agent1", label: "Agent 1 — Document Processing" },
  { key: "agent2", label: "Agent 2 — Credit Analysis" },
  { key: "agent3", label: "Agent 3 — Fraud Detection" },
  { key: "agent4", label: "Agent 4 — Compliance Check" },
  { key: "agent5", label: "Agent 5 — Decision Orchestrator" },
];

const DOC_TYPES = [
  { keyword: "income_statement",     label: "Income Statement",     accept: ".pdf" },
  { keyword: "balance_sheet",        label: "Balance Sheet",        accept: ".pdf" },
  { keyword: "financial_statements", label: "Financial Statements", accept: ".xlsx,.xls" },
  { keyword: "financial_summary",    label: "Financial Summary",    accept: ".csv" },
  { keyword: "application_proposal", label: "Application Proposal", accept: ".pdf" },
];

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AgentResult = Record<string, any>;

interface PipelineResult {
  agent1?: AgentResult; agent2?: AgentResult; agent3?: AgentResult;
  agent4?: AgentResult; agent5?: AgentResult; final_state?: string;
}

interface DocFile { name: string; size: number; ext: string }

function fmt(bytes: number) {
  return bytes < 1024 ? `${bytes} B` : bytes < 1048576 ? `${(bytes / 1024).toFixed(1)} KB` : `${(bytes / 1048576).toFixed(1)} MB`;
}

function extIcon(ext: string) {
  if (ext === ".pdf")  return { label: "PDF",  cls: "bg-red-900 text-red-300" };
  if (ext === ".xlsx" || ext === ".xls") return { label: "XLS", cls: "bg-green-900 text-green-300" };
  if (ext === ".csv")  return { label: "CSV",  cls: "bg-blue-900 text-blue-300" };
  return { label: ext.slice(1).toUpperCase(), cls: "bg-gray-800 text-gray-400" };
}

function matchesType(filename: string, keyword: string) {
  return filename.toLowerCase().includes(keyword);
}

function AgentCard({ agentKey, label, data }: { agentKey: string; label: string; data: AgentResult | undefined }) {
  const [open, setOpen] = useState(false);
  if (!data) return (
    <div className="border border-gray-800 rounded-lg p-3 opacity-30">
      <p className="text-gray-500 text-xs">{label}</p>
      <p className="text-gray-700 text-xs mt-1">Not yet run</p>
    </div>
  );
  const highlights: { label: string; value: string }[] = [];
  if (agentKey === "agent1") {
    highlights.push(
      { label: "Confidence",      value: String(data.quality?.overall_confidence ?? "—") },
      { label: "Coherent",        value: String(data.quality?.is_coherent ?? "—") },
      { label: "Missing Fields",  value: (data.critical_missing_fields ?? []).join(", ") || "none" },
      { label: "Re-extraction",   value: String(data.reextraction_recommended ?? "—") },
    );
  } else if (agentKey === "agent2") {
    highlights.push(
      { label: "Risk Tier",        value: String(data.risk_tier ?? "—") },
      { label: "Confidence",       value: String(data.confidence_score ?? "—") },
      { label: "Recommended Limit", value: data.recommended_limit_usd != null ? "$" + Number(data.recommended_limit_usd).toLocaleString() : "—" },
      { label: "Rationale",        value: String(data.rationale ?? "—") },
    );
  } else if (agentKey === "agent3") {
    highlights.push(
      { label: "Fraud Score",     value: String(data.fraud_score ?? "—") },
      { label: "Risk Level",      value: String(data.risk_level ?? "—") },
      { label: "Anomalies Found", value: String(data.anomalies_found ?? "—") },
      { label: "Recommendation",  value: String(data.recommendation ?? "—") },
    );
  } else if (agentKey === "agent4") {
    highlights.push(
      { label: "Verdict",       value: String(data.overall_verdict ?? "—") },
      { label: "Hard Block",    value: String(data.has_hard_block ?? "—") },
      { label: "Rules Passed",  value: `${data.rules_passed ?? "—"} / ${data.rules_evaluated ?? "—"}` },
      { label: "Summary",       value: String(data.summary ?? "—") },
    );
  } else if (agentKey === "agent5") {
    highlights.push(
      { label: "Recommendation",   value: String(data.recommendation ?? "—") },
      { label: "Confidence",       value: String(data.confidence_score ?? "—") },
      { label: "Override Applied", value: String(data.override_applied ?? "—") },
      { label: "Final State",      value: String(data.final_state ?? "—") },
    );
  }
  const stateColor = (v: string) =>
    v === "FINAL_APPROVED" ? "text-green-400" :
    v === "FINAL_DECLINED" || v === "DECLINED_COMPLIANCE" ? "text-red-400" : "text-yellow-400";
  return (
    <div className="border border-gray-700 rounded-lg p-3 bg-gray-900/60">
      <div className="flex items-center justify-between mb-2">
        <p className={`text-xs font-semibold ${agentKey === "agent5" ? stateColor(String(data.final_state ?? "")) : "text-indigo-300"}`}>
          {label}
        </p>
        <button onClick={() => setOpen((o) => !o)} className="text-gray-600 hover:text-gray-400 text-xs">
          {open ? "▲ hide" : "▼ raw"}
        </button>
      </div>
      <div className="space-y-1">
        {highlights.map((h) => (
          <div key={h.label} className="flex justify-between text-xs gap-4">
            <span className="text-gray-600 shrink-0">{h.label}</span>
            <span className="text-gray-300 text-right truncate max-w-xs">{h.value}</span>
          </div>
        ))}
      </div>
      {open && (
        <pre className="mt-3 text-xs text-gray-500 bg-black/40 rounded p-2 overflow-x-auto whitespace-pre-wrap max-h-48 overflow-y-auto">
          {JSON.stringify(data, null, 2)}
        </pre>
      )}
    </div>
  );
}

export default function PipelineTab() {
  const [companyId, setCompanyId]         = useState("COMP-002");
  const [amount, setAmount]               = useState("500000");
  const [appId, setAppId]                 = useState("");
  const [status, setStatus]               = useState<string>("idle");
  const [lines, setLines]                 = useState<string[]>([]);
  const [result, setResult]               = useState<PipelineResult | null>(null);
  const [error, setError]                 = useState<string | null>(null);
  const [polling, setPolling]             = useState(false);
  const [generatedAppId, setGeneratedAppId] = useState<string | null>(null);

  // Document management
  const [docs, setDocs]                   = useState<DocFile[]>([]);
  const [docsLoading, setDocsLoading]     = useState(false);
  const [uploading, setUploading]         = useState<string | null>(null);
  const [uploadError, setUploadError]     = useState<string | null>(null);
  const fileInputRef                      = useRef<HTMLInputElement>(null);
  const [pendingKeyword, setPendingKeyword] = useState<string | null>(null);

  // Load documents when company changes
  useEffect(() => {
    setDocsLoading(true);
    setUploadError(null);
    api.listDocuments(companyId)
      .then(setDocs)
      .catch(() => setDocs([]))
      .finally(() => setDocsLoading(false));
  }, [companyId]);

  function openFilePicker(keyword: string) {
    setPendingKeyword(keyword);
    fileInputRef.current?.click();
  }

  async function handleFileChosen(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file || !pendingKeyword) return;
    e.target.value = "";
    setUploading(pendingKeyword);
    setUploadError(null);
    try {
      await api.uploadDocument(companyId, file);
      const updated = await api.listDocuments(companyId);
      setDocs(updated);
    } catch (err) {
      setUploadError(String(err));
    } finally {
      setUploading(null);
      setPendingKeyword(null);
    }
  }

  async function deleteDoc(filename: string) {
    try {
      await api.deleteDocument(companyId, filename);
      setDocs((prev) => prev.filter((d) => d.name !== filename));
    } catch (err) {
      setUploadError(String(err));
    }
  }

  async function run() {
    const amt = parseFloat(amount);
    if (!companyId.trim() || isNaN(amt) || amt <= 0) return;
    setLines([]); setResult(null); setError(null);
    setGeneratedAppId(null); setStatus("starting"); setPolling(true);
    try {
      const data = await api.runPipeline(companyId.trim(), amt, appId.trim() || undefined);
      setGeneratedAppId(data.app_id);
      poll(data.job_id);
    } catch (e) {
      setError(String(e)); setStatus("error"); setPolling(false);
    }
  }

  function poll(id: string) {
    const interval = setInterval(async () => {
      try {
        const data = await api.pipelineStatus(id) as {
          status: string; lines?: string[];
          result?: PipelineResult; error?: string;
        };
        setStatus(data.status);
        setLines(data.lines ?? []);
        if (data.result != null) setResult(typeof data.result === "object" ? data.result as PipelineResult : null);
        if (data.error) setError(String(data.error));
        if (data.status === "done" || data.status === "error") { clearInterval(interval); setPolling(false); }
      } catch { clearInterval(interval); setPolling(false); }
    }, 1000);
  }

  const statusColor =
    status === "done"  ? "text-green-400" :
    status === "error" ? "text-red-400"   :
    status === "idle"  ? "text-gray-600"  : "text-yellow-400";

  const finalState = result?.final_state;
  const finalColor =
    finalState === "FINAL_APPROVED"      ? "bg-green-900 text-green-300"   :
    finalState === "FINAL_DECLINED"      ? "bg-red-900 text-red-300"       :
    finalState === "DECLINED_COMPLIANCE" ? "bg-orange-900 text-orange-300" :
    "bg-gray-800 text-gray-300";

  return (
    <div className="space-y-5">
      <h2 className="text-white text-sm font-semibold tracking-widest uppercase">Live Pipeline Runner</h2>
      <p className="text-gray-600 text-xs">
        Runs all 5 agents: Document Processing → Credit Analysis → Fraud Detection → Compliance → Decision
      </p>

      {/* ── Pipeline config ── */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        <div className="flex flex-col gap-1">
          <label className="text-gray-600 text-xs">Company ID</label>
          <select
            value={companyId}
            onChange={(e) => setCompanyId(e.target.value)}
            className="bg-gray-900 border border-gray-700 rounded px-3 py-2 text-xs text-gray-300 focus:outline-none focus:border-indigo-500"
          >
            {COMPANIES.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        </div>
        <div className="flex flex-col gap-1">
          <label className="text-gray-600 text-xs">Loan Amount (USD)</label>
          <input
            type="number" value={amount} onChange={(e) => setAmount(e.target.value)}
            min={1000} step={10000}
            className="bg-gray-900 border border-gray-700 rounded px-3 py-2 text-xs text-gray-300 font-mono focus:outline-none focus:border-indigo-500"
          />
        </div>
        <div className="flex flex-col gap-1">
          <label className="text-gray-600 text-xs">Application ID <span className="text-gray-700">(optional)</span></label>
          <input
            value={appId} onChange={(e) => setAppId(e.target.value)}
            placeholder="APEX-PIPE-XXXXXX"
            className="bg-gray-900 border border-gray-700 rounded px-3 py-2 text-xs text-gray-300 font-mono placeholder-gray-700 focus:outline-none focus:border-indigo-500"
          />
        </div>
      </div>

      {/* ── Document Manager ── */}
      <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
        <div className="flex items-center justify-between px-4 py-3 border-b border-gray-800">
          <div>
            <span className="text-gray-300 text-xs font-semibold">Documents for {companyId}</span>
            <p className="text-gray-600 text-xs mt-0.5">
              Upload PDF / Excel / CSV files. Names must contain the document type keyword.
            </p>
          </div>
          {docsLoading && <span className="text-gray-600 text-xs animate-pulse">Loading…</span>}
        </div>

        {/* Hidden file input */}
        <input ref={fileInputRef} type="file" accept=".pdf,.xlsx,.xls,.csv" className="hidden" onChange={handleFileChosen} />

        {uploadError && (
          <div className="px-4 py-2 bg-red-950 border-b border-red-800 text-red-400 text-xs">{uploadError}</div>
        )}

        {/* Document type rows */}
        <div className="divide-y divide-gray-800">
          {DOC_TYPES.map((dt) => {
            const existing = docs.filter((d) => matchesType(d.name, dt.keyword));
            const isUploading = uploading === dt.keyword;
            return (
              <div key={dt.keyword} className="flex items-center gap-3 px-4 py-3">
                {/* Type label */}
                <div className="w-44 shrink-0">
                  <p className="text-gray-300 text-xs font-semibold">{dt.label}</p>
                  <p className="text-gray-600 text-xs font-mono">{dt.keyword}.*</p>
                </div>

                {/* Files */}
                <div className="flex-1 flex flex-wrap gap-2 min-h-[28px]">
                  {existing.length === 0 && !isUploading && (
                    <span className="text-gray-700 text-xs italic">No file</span>
                  )}
                  {existing.map((f) => {
                    const icon = extIcon(f.ext);
                    return (
                      <div key={f.name} className="flex items-center gap-1.5 bg-gray-800 rounded px-2 py-1">
                        <span className={`text-xs px-1 py-0.5 rounded font-bold ${icon.cls}`}>{icon.label}</span>
                        <span className="text-gray-300 text-xs font-mono max-w-48 truncate">{f.name}</span>
                        <span className="text-gray-600 text-xs">{fmt(f.size)}</span>
                        <button
                          onClick={() => deleteDoc(f.name)}
                          className="text-gray-700 hover:text-red-400 text-xs ml-1 transition-colors"
                          title="Delete"
                        >✕</button>
                      </div>
                    );
                  })}
                  {isUploading && (
                    <span className="text-indigo-400 text-xs animate-pulse">Uploading…</span>
                  )}
                </div>

                {/* Upload button */}
                <button
                  onClick={() => openFilePicker(dt.keyword)}
                  disabled={isUploading}
                  className="shrink-0 px-3 py-1.5 bg-gray-800 hover:bg-gray-700 disabled:opacity-40 rounded text-xs text-gray-300 transition-colors border border-gray-700"
                >
                  {existing.length > 0 ? "↑ Replace" : "↑ Upload"}
                </button>
              </div>
            );
          })}
        </div>

        {/* Summary footer */}
        <div className="px-4 py-2 border-t border-gray-800 flex items-center gap-2">
          <span className={`text-xs ${docs.length >= 5 ? "text-green-400" : "text-yellow-400"}`}>
            {docs.length} / 5 required files present
          </span>
          {docs.length < 5 && (
            <span className="text-gray-600 text-xs">— Pipeline requires all 5 document types</span>
          )}
          {docs.length >= 5 && (
            <span className="text-green-600 text-xs">— Ready to run</span>
          )}
        </div>
      </div>

      {/* ── Run button ── */}
      <button
        onClick={run}
        disabled={polling}
        className="px-5 py-2 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 rounded text-xs text-white font-semibold transition-colors"
      >
        {polling ? "▶ Running pipeline…" : "▶ Run Full Pipeline"}
      </button>

      {/* ── Pipeline output ── */}
      {status !== "idle" && (
        <div className="space-y-3">
          <div className="flex items-center gap-3 text-xs">
            <span className="text-gray-600">Status:</span>
            <span className={`font-semibold uppercase ${statusColor}`}>{status}</span>
            {generatedAppId && (
              <span className="text-gray-600 font-mono ml-2">app: <span className="text-indigo-300">{generatedAppId}</span></span>
            )}
            {polling && <span className="text-yellow-400 animate-pulse ml-auto">● Live</span>}
            {finalState && (
              <span className={`ml-auto px-2 py-0.5 rounded text-xs font-bold ${finalColor}`}>{finalState}</span>
            )}
          </div>

          {lines.length > 0 && (
            <pre className="text-xs text-gray-400 bg-black/40 rounded p-3 overflow-x-auto max-h-52 overflow-y-auto whitespace-pre-wrap font-mono">
              {lines.join("\n")}
            </pre>
          )}

          {result && (
            <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
              {AGENTS.map(({ key, label }) => (
                <AgentCard key={key} agentKey={key} label={label} data={result[key as keyof PipelineResult] as AgentResult | undefined} />
              ))}
            </div>
          )}

          {error && (
            <div className="bg-red-950 border border-red-800 rounded p-3">
              <p className="text-red-400 text-xs font-semibold mb-1">Error</p>
              <pre className="text-xs text-red-300 whitespace-pre-wrap">{error}</pre>
            </div>
          )}
        </div>
      )}

      {status === "idle" && (
        <div className="text-center py-8 text-gray-700 text-xs border border-dashed border-gray-800 rounded-lg">
          Upload documents above, then click ▶ Run Full Pipeline.
        </div>
      )}
    </div>
  );
}
