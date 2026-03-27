"use client";
import { useState } from "react";
import { api } from "../../api";

const STEPS = [
  {
    id: "1",
    label: "The Week Standard",
    desc: "Core event store validation: append, read, snapshot, and full replay of the event log.",
    icon: "①",
    color: "text-indigo-400",
    border: "border-indigo-800",
    bg: "bg-indigo-950/40",
    btn: "bg-indigo-600 hover:bg-indigo-500",
  },
  {
    id: "2",
    label: "Concurrency Under Pressure",
    desc: "Optimistic concurrency control stress-test: parallel writers competing on the same stream.",
    icon: "②",
    color: "text-cyan-400",
    border: "border-cyan-800",
    bg: "bg-cyan-950/40",
    btn: "bg-indigo-600 hover:bg-indigo-500",
  },
  {
    id: "3",
    label: "Temporal Compliance Query",
    desc: "Point-in-time regulatory audit: reconstruct exactly what the system knew at any past timestamp.",
    icon: "③",
    color: "text-indigo-400",
    border: "border-indigo-800",
    bg: "bg-indigo-950/40",
    btn: "bg-indigo-600 hover:bg-indigo-500",
  },
  {
    id: "4",
    label: "Schema Upcasting",
    desc: "Event versioning and transparent upcasting: old events seamlessly migrate to the current schema.",
    icon: "④",
    color: "text-purple-400",
    border: "border-purple-800",
    bg: "bg-purple-950/40",
    btn: "bg-indigo-600 hover:bg-indigo-500",
  },
  {
    id: "5",
    label: "Gas Town Integrity",
    desc: "Agent crash recovery and causal chain reconstruction after mid-pipeline failure.",
    icon: "⑤",
    color: "text-indigo-400",
    border: "border-indigo-800",
    bg: "bg-indigo-950/40",
    btn: "bg-indigo-600 hover:bg-indigo-500",
  },
  {
    id: "6",
    label: "What-If Counterfactual",
    desc: "Counterfactual replay: branch the event log and simulate alternative decision outcomes.",
    icon: "⑥",
    color: "text-green-400",
    border: "border-green-800",
    bg: "bg-green-950/40",
    btn: "bg-indigo-600 hover:bg-indigo-500",
  },
];

type RunStatus = "idle" | "running" | "done" | "error";

interface RunState {
  status: RunStatus;
  lines: string[];
  result?: string;
  error?: string;
}

const IDLE: RunState = { status: "idle", lines: [] };

export default function DemoSuiteTab() {
  const [runs, setRuns] = useState<Record<string, RunState>>(
    Object.fromEntries(STEPS.map((s) => [s.id, IDLE]))
  );
  const [activeOutput, setActiveOutput] = useState<string | null>(null);

  function patch(id: string, update: Partial<RunState>) {
    setRuns((prev) => ({ ...prev, [id]: { ...prev[id], ...update } }));
  }

  async function runStep(id: string) {
    patch(id, { status: "running", lines: [], result: undefined, error: undefined });
    setActiveOutput(id);
    try {
      const data = await api.runDemo(id);
      const jobId = data.job_id;
      const interval = setInterval(async () => {
        try {
          const s = await api.demoStatus(jobId) as {
            status: string; lines?: string[]; result?: string; error?: string;
          };
          patch(id, {
            lines: s.lines ?? [],
            ...(s.result != null ? { result: s.result } : {}),
            ...(s.error  != null ? { error:  s.error  } : {}),
            status: (s.status === "done" || s.status === "error")
              ? (s.status as RunStatus)
              : "running",
          });
          if (s.status === "done" || s.status === "error") clearInterval(interval);
        } catch {
          clearInterval(interval);
          patch(id, { status: "error", error: "Polling failed" });
        }
      }, 800);
    } catch (e) {
      patch(id, { status: "error", error: String(e) });
    }
  }

  const step = activeOutput ? STEPS.find((s) => s.id === activeOutput) : null;
  const active = activeOutput ? runs[activeOutput] : null;

  return (
    <div className="flex flex-col gap-5">
      <div>
        <h2 className="text-white text-sm font-semibold tracking-widest uppercase">Demo Suite</h2>
        <p className="text-gray-600 text-xs mt-1">
          Run any scenario live against the event store. Click ▶ Run on a step to execute it.
        </p>
      </div>

      {/* ── Step cards — each with its own Run button ── */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        {STEPS.map((s) => {
          const r = runs[s.id] ?? IDLE;
          const isRunning = r.status === "running";
          const statusBadge =
            r.status === "done"    ? "bg-green-900 text-green-400"           :
            r.status === "error"   ? "bg-red-900 text-red-400"               :
            r.status === "running" ? "bg-yellow-900 text-yellow-400 animate-pulse" :
            null;

          return (
            <div
              key={s.id}
              className={`rounded-lg border p-4 transition-all ${
                activeOutput === s.id ? `${s.bg} ${s.border}` : "bg-gray-900 border-gray-800"
              }`}
            >
              {/* Header */}
              <div className="flex items-start justify-between gap-3">
                <div className="flex items-center gap-2 min-w-0">
                  <span className={`text-xl font-bold shrink-0 ${s.color}`}>{s.icon}</span>
                  <div className="min-w-0">
                    <p className={`text-xs font-semibold ${s.color}`}>{s.label}</p>
                    <p className="text-gray-600 text-xs mt-0.5 leading-relaxed">{s.desc}</p>
                  </div>
                </div>

                {/* Run / status */}
                <div className="flex items-center gap-2 shrink-0">
                  {statusBadge && (
                    <span className={`text-xs px-2 py-0.5 rounded font-bold uppercase ${statusBadge}`}>
                      {r.status}
                    </span>
                  )}
                  <button
                    onClick={() => runStep(s.id)}
                    disabled={isRunning}
                    className={`px-4 py-1.5 rounded text-xs font-semibold text-white transition-colors disabled:opacity-50 ${s.btn}`}
                  >
                    {isRunning ? "…" : "▶ Run"}
                  </button>
                </div>
              </div>

              {/* Inline peek: last 3 log lines while running */}
              {isRunning && r.lines.length > 0 && (
                <pre className="mt-3 text-xs text-gray-500 font-mono bg-black/30 rounded p-2 overflow-hidden whitespace-pre-wrap line-clamp-3">
                  {r.lines.slice(-3).join("\n")}
                </pre>
              )}

              {/* View output link */}
              {(r.status === "done" || r.status === "error") && (
                <button
                  onClick={() => setActiveOutput(activeOutput === s.id ? null : s.id)}
                  className={`mt-3 text-xs underline underline-offset-2 ${s.color} hover:opacity-80`}
                >
                  {activeOutput === s.id ? "▲ Hide output" : "▼ View full output"}
                </button>
              )}
            </div>
          );
        })}
      </div>

      {/* ── Full output panel ── */}
      {step && active && active.status !== "idle" && activeOutput && (
        <div className={`rounded-lg border ${step.border} overflow-hidden`}>
          {/* Panel header */}
          <div className={`flex items-center justify-between px-4 py-3 border-b ${step.border} ${step.bg}`}>
            <div className="flex items-center gap-2">
              <span className={`text-base font-bold ${step.color}`}>{step.icon}</span>
              <span className={`text-xs font-semibold ${step.color}`}>{step.label} — Output</span>
            </div>
            <div className="flex items-center gap-2">
              {active.status === "running" && (
                <span className="flex items-center gap-1.5 text-xs text-yellow-400">
                  <span className="w-2 h-2 rounded-full bg-yellow-400 animate-pulse" /> Live
                </span>
              )}
              {active.status === "done" && (
                <span className="bg-green-900 text-green-400 text-xs px-2 py-0.5 rounded font-bold">✓ DONE</span>
              )}
              {active.status === "error" && (
                <span className="bg-red-900 text-red-400 text-xs px-2 py-0.5 rounded font-bold">✗ ERROR</span>
              )}
              <button
                onClick={() => setActiveOutput(null)}
                className="text-gray-600 hover:text-gray-400 text-xs ml-2"
              >
                ✕ Close
              </button>
            </div>
          </div>

          {/* Live log */}
          {active.lines.length > 0 && (
            <pre className="p-4 text-xs text-gray-300 font-mono bg-gray-950 overflow-x-auto overflow-y-auto max-h-[55vh] whitespace-pre-wrap leading-relaxed">
              {active.lines.join("\n")}
            </pre>
          )}

          {/* Result */}
          {active.result && (
            <div className={`border-t ${step.border}`}>
              <div className={`px-4 py-2 ${step.bg} flex items-center gap-2`}>
                <span className={`text-xs font-semibold tracking-widest uppercase ${step.color}`}>Result</span>
                <span className="ml-auto bg-green-900 text-green-400 text-xs px-2 py-0.5 rounded font-bold">✓ PASSED</span>
              </div>
              <pre className={`p-4 text-xs font-mono bg-gray-950 overflow-x-auto whitespace-pre-wrap leading-relaxed ${step.color}`}>
                {active.result}
              </pre>
            </div>
          )}

          {/* Error */}
          {active.error && (
            <div className="border-t border-red-800">
              <div className="px-4 py-2 bg-red-950/60">
                <span className="text-xs font-semibold tracking-widest uppercase text-red-400">Error</span>
              </div>
              <pre className="p-4 text-xs text-red-300 font-mono bg-gray-950 overflow-x-auto whitespace-pre-wrap leading-relaxed">
                {active.error}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
