"use client";
import { useEffect, useState, useCallback } from "react";
import {
  BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid,
  PieChart, Pie, Cell, LineChart, Line, Legend,
} from "recharts";
import { api } from "../../api";

function num(v: unknown) { return v != null ? Number(v).toLocaleString() : "—"; }
function pct(v: unknown) { return v != null ? Number(v).toFixed(1) + "%" : "—"; }
function score(v: unknown) { return v != null ? Number(v).toFixed(3) : "—"; }
function money(v: unknown) { return v != null ? "$" + Number(v).toLocaleString() : "—"; }
function ms(v: unknown) { return v != null ? Number(v).toFixed(0) + " ms" : "—"; }

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type D = Record<string, any>;

const PALETTE = [
  "#6366f1","#22d3ee","#a78bfa","#34d399","#f87171",
  "#fbbf24","#60a5fa","#e879f9","#4ade80","#fb923c",
];

const TOOLTIP_STYLE = {
  contentStyle: { background: "#1f2937", border: "1px solid #374151", borderRadius: 6, fontSize: 11 },
  labelStyle: { color: "#9ca3af" },
  itemStyle: { color: "#e5e7eb" },
};

function SectionCard({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
      <h3 className="text-gray-400 text-xs font-semibold tracking-widest uppercase mb-3 border-b border-gray-800 pb-2">
        {title}
      </h3>
      {children}
    </div>
  );
}

function KvRows({ rows }: { rows: { label: string; value: string }[] }) {
  return (
    <div className="space-y-2">
      {rows.map((r) => (
        <div key={r.label} className="flex justify-between items-center text-xs">
          <span className="text-gray-500">{r.label}</span>
          <span className="text-white font-mono font-semibold">{r.value}</span>
        </div>
      ))}
    </div>
  );
}

interface Checkpoint {
  projection_name: string;
  last_position: number;
  latest_global: number;
  lag_events: number;
  updated_at: string | null;
}

export default function AnalysisTab() {
  const [data, setData]               = useState<D | null>(null);
  const [loading, setLoading]         = useState(true);
  const [error, setError]             = useState<string | null>(null);
  const [checkpoints, setCheckpoints] = useState<Checkpoint[]>([]);
  const [cpLoading, setCpLoading]     = useState(true);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      setData(await api.analysis() as D);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }

  const refreshCheckpoints = useCallback(async () => {
    setCpLoading(true);
    try {
      setCheckpoints(await api.checkpoints());
    } catch {
      // non-fatal
    } finally {
      setCpLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    refreshCheckpoints();
    const id = setInterval(refreshCheckpoints, 5000);
    return () => clearInterval(id);
  }, [refreshCheckpoints]);

  if (loading) return <div className="text-center py-20 text-gray-600 text-xs animate-pulse">Loading analysis across all 12 database tables…</div>;
  if (error)   return <div className="bg-red-950 border border-red-800 rounded-lg p-4 text-red-400 text-xs">{error}</div>;
  if (!data)   return null;

  const s: D  = data.summary ?? {};
  const sr: D = data.stream_registry ?? {};
  const ob: D = data.outbox_summary ?? {};
  const af: D = data.app_financials ?? {};
  const ag: D = data.agent_summary ?? {};
  const ca: D = data.compliance_audit_summary ?? {};
  const cs: D = data.compliance_snapshots_summary ?? {};
  const reg: D = data.registry_summary ?? {};
  const fl: D = data.flags_summary ?? {};
  const fin: D = data.financials_summary ?? {};
  const ln: D = data.loan_summary ?? {};

  // Chart data
  const eventTypeDist: D[] = (data.event_type_dist ?? []).map((r: D) => ({
    name: String(r.event_type ?? "").replace(/([A-Z])/g, " $1").trim(),
    count: Number(r.count ?? 0),
    pct: Number(r.pct ?? 0),
  }));

  const dailyVolume: D[] = (data.daily_volume ?? []).map((r: D) => ({
    day: String(r.day ?? "").slice(5),  // MM-DD
    count: Number(r.count ?? 0),
  }));

  const riskTierDist: D[] = (data.risk_tier_dist ?? []).filter((r: D) => r.risk_tier).map((r: D) => ({
    name: String(r.risk_tier),
    value: Number(r.count ?? 0),
  }));

  const appFunnel: D[] = (data.application_funnel ?? []).map((r: D) => ({
    name: String(r.state ?? "").replace(/_/g, " "),
    count: Number(r.count ?? 0),
  }));

  const rulePassRates: D[] = (data.rule_pass_rates ?? []).map((r: D) => ({
    name: String(r.rule_id ?? ""),
    passed: Number(r.passed ?? 0),
    failed: Number(r.failed ?? 0),
    rate: Number(r.pass_rate_pct ?? 0),
  }));

  const loanStatusDist: D[] = (data.loan_status_dist ?? []).filter((r: D) => r.status).map((r: D) => ({
    name: String(r.status),
    value: Number(r.count ?? 0),
  }));

  const topAgents: D[] = (data.top_agents ?? []).map((r: D) => ({
    name: String(r.agent_id ?? "").slice(-8),
    analyses: Number(r.analyses_completed ?? 0),
    decisions: Number(r.decisions_generated ?? 0),
    confidence: Number(r.avg_confidence ?? 0),
  }));

  const sectorRevenue: D[] = (data.revenue_by_sector ?? [])
    .filter((r: D) => r.sector && r.avg_revenue)
    .slice(0, 8)
    .map((r: D) => ({
      name: String(r.sector ?? ""),
      revenue: Number(r.avg_revenue ?? 0),
      ebitda: Number(r.avg_ebitda_margin ?? 0),
    }));

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-white text-sm font-semibold tracking-widest uppercase">Analysis Report</h2>
        <button onClick={load} className="px-3 py-1.5 bg-gray-800 hover:bg-gray-700 rounded text-xs">↻ Refresh</button>
      </div>

      {/* ── Projection Checkpoints (live, polls every 5s) ── */}
      <div className="bg-gray-900 border border-gray-800 rounded-lg overflow-hidden">
        <div className="flex items-center justify-between px-4 py-3 border-b border-gray-800">
          <div>
            <span className="text-gray-300 text-xs font-semibold">Projection Checkpoints</span>
            <span className="text-gray-600 text-xs ml-2">public.projection_checkpoints · auto-refresh 5s</span>
          </div>
          <div className="flex items-center gap-2">
            {cpLoading
              ? <span className="w-2 h-2 rounded-full bg-yellow-400 animate-pulse" />
              : <span className="w-2 h-2 rounded-full bg-green-500" />}
            <button onClick={refreshCheckpoints} className="text-gray-600 hover:text-gray-400 text-xs">↻</button>
          </div>
        </div>
        <table className="w-full text-xs">
          <thead>
            <tr className="text-gray-600 border-b border-gray-800">
              <th className="text-left px-4 py-2 font-normal tracking-wider">Projection</th>
              <th className="text-right px-4 py-2 font-normal tracking-wider">Last Position</th>
              <th className="text-right px-4 py-2 font-normal tracking-wider">Latest Global</th>
              <th className="text-right px-4 py-2 font-normal tracking-wider">Lag (events)</th>
              <th className="text-left px-4 py-2 font-normal tracking-wider">Updated At</th>
              <th className="text-left px-4 py-2 font-normal tracking-wider">Status</th>
            </tr>
          </thead>
          <tbody>
            {checkpoints.length === 0 ? (
              <tr><td colSpan={6} className="px-4 py-6 text-center text-gray-700">No checkpoint data</td></tr>
            ) : checkpoints.map((cp) => {
              const lagPct = cp.latest_global > 0 ? (cp.lag_events / cp.latest_global) * 100 : 0;
              const lagColor =
                cp.lag_events === 0  ? "text-green-400"  :
                cp.lag_events < 10   ? "text-yellow-400" :
                cp.lag_events < 100  ? "text-orange-400" : "text-red-400";
              const statusLabel =
                cp.lag_events === 0  ? "UP TO DATE"  :
                cp.lag_events < 10   ? "NEAR LIVE"   :
                cp.lag_events < 100  ? "LAGGING"     : "BEHIND";
              const statusCls =
                cp.lag_events === 0  ? "bg-green-900 text-green-400"   :
                cp.lag_events < 10   ? "bg-yellow-900 text-yellow-400" :
                cp.lag_events < 100  ? "bg-orange-900 text-orange-400" : "bg-red-900 text-red-400";
              return (
                <tr key={cp.projection_name} className="border-b border-gray-800/40 hover:bg-gray-800/30">
                  <td className="px-4 py-2.5 text-indigo-300 font-mono">{cp.projection_name}</td>
                  <td className="px-4 py-2.5 text-right text-gray-300 font-mono">{cp.last_position.toLocaleString()}</td>
                  <td className="px-4 py-2.5 text-right text-gray-400 font-mono">{cp.latest_global.toLocaleString()}</td>
                  <td className="px-4 py-2.5 text-right">
                    <div className="flex items-center justify-end gap-2">
                      <div className="w-20 h-1.5 bg-gray-800 rounded-full overflow-hidden">
                        <div
                          className={`h-full rounded-full transition-all ${
                            cp.lag_events === 0 ? "bg-green-500" :
                            cp.lag_events < 10  ? "bg-yellow-500" :
                            cp.lag_events < 100 ? "bg-orange-500" : "bg-red-500"
                          }`}
                          style={{ width: `${Math.min(lagPct, 100)}%` }}
                        />
                      </div>
                      <span className={`font-mono font-semibold ${lagColor}`}>{cp.lag_events}</span>
                    </div>
                  </td>
                  <td className="px-4 py-2.5 text-gray-600 font-mono">
                    {cp.updated_at ? new Date(cp.updated_at).toLocaleString() : "—"}
                  </td>
                  <td className="px-4 py-2.5">
                    <span className={`px-2 py-0.5 rounded text-xs font-bold ${statusCls}`}>{statusLabel}</span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {/* ── Row 1: Event store KPIs + daily volume line ── */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <SectionCard title="Event Store — public.events">
          <KvRows rows={[
            { label: "Total Events",    value: num(s.total_events) },
            { label: "Distinct Streams", value: num(s.total_streams) },
            { label: "Today's Events",  value: num(s.today_events) },
            { label: "Last Hour",       value: num(s.last_hour_events) },
            { label: "First Event",     value: s.first_event_at ? new Date(s.first_event_at).toLocaleString() : "—" },
            { label: "Last Event",      value: s.last_event_at  ? new Date(s.last_event_at).toLocaleString()  : "—" },
          ]} />
        </SectionCard>

        <SectionCard title="Daily Event Volume (14 days)">
          {dailyVolume.length > 0 ? (
            <ResponsiveContainer width="100%" height={180}>
              <LineChart data={dailyVolume} margin={{ top: 4, right: 8, bottom: 0, left: -20 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
                <XAxis dataKey="day" tick={{ fill: "#4b5563", fontSize: 10 }} />
                <YAxis tick={{ fill: "#4b5563", fontSize: 10 }} />
                <Tooltip {...TOOLTIP_STYLE} />
                <Line type="monotone" dataKey="count" stroke="#6366f1" strokeWidth={2} dot={false} />
              </LineChart>
            </ResponsiveContainer>
          ) : <p className="text-gray-600 text-xs py-8 text-center">No data</p>}
        </SectionCard>
      </div>

      {/* ── Row 2: Event type distribution bar ── */}
      <SectionCard title="Event Type Distribution">
        {eventTypeDist.length > 0 ? (
          <ResponsiveContainer width="100%" height={220}>
            <BarChart data={eventTypeDist} margin={{ top: 4, right: 8, bottom: 60, left: -20 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
              <XAxis dataKey="name" tick={{ fill: "#4b5563", fontSize: 9 }} angle={-30} textAnchor="end" interval={0} />
              <YAxis tick={{ fill: "#4b5563", fontSize: 10 }} />
              <Tooltip {...TOOLTIP_STYLE} />
              <Bar dataKey="count" radius={[3,3,0,0]}>
                {eventTypeDist.map((_: D, i: number) => (
                  <Cell key={i} fill={PALETTE[i % PALETTE.length]} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        ) : <p className="text-gray-600 text-xs py-8 text-center">No data</p>}
      </SectionCard>

      {/* ── Row 3: Application funnel + Risk tier pie ── */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <SectionCard title="Application Pipeline Funnel">
          {appFunnel.length > 0 ? (
            <ResponsiveContainer width="100%" height={200}>
              <BarChart layout="vertical" data={appFunnel} margin={{ top: 4, right: 20, bottom: 0, left: 90 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
                <XAxis type="number" tick={{ fill: "#4b5563", fontSize: 10 }} />
                <YAxis type="category" dataKey="name" tick={{ fill: "#9ca3af", fontSize: 9 }} width={90} />
                <Tooltip {...TOOLTIP_STYLE} />
                <Bar dataKey="count" radius={[0,3,3,0]}>
                  {appFunnel.map((_: D, i: number) => <Cell key={i} fill={PALETTE[i % PALETTE.length]} />)}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          ) : <p className="text-gray-600 text-xs py-8 text-center">No data</p>}
        </SectionCard>

        <SectionCard title="Risk Tier Distribution">
          {riskTierDist.length > 0 ? (
            <div className="flex flex-col items-center">
              <ResponsiveContainer width="100%" height={180}>
                <PieChart>
                  <Pie data={riskTierDist} dataKey="value" nameKey="name" cx="50%" cy="50%" outerRadius={65}>
                    {riskTierDist.map((_: D, i: number) => <Cell key={i} fill={PALETTE[i % PALETTE.length]} />)}
                  </Pie>
                  <Tooltip {...TOOLTIP_STYLE} />
                  <Legend wrapperStyle={{ fontSize: 10, color: "#9ca3af" }} />
                </PieChart>
              </ResponsiveContainer>
            </div>
          ) : <p className="text-gray-600 text-xs py-8 text-center">No data</p>}
        </SectionCard>
      </div>

      {/* ── Row 4: App financials + Agent performance KPIs ── */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <SectionCard title="Application Financials">
          <KvRows rows={[
            { label: "Avg Requested", value: money(af.avg_requested_usd) },
            { label: "Avg Approved",  value: money(af.avg_approved_usd) },
            { label: "Max Requested", value: money(af.max_requested_usd) },
            { label: "Avg Fraud Score", value: score(af.avg_fraud_score) },
          ]} />
        </SectionCard>

        <SectionCard title="Agent Performance">
          <KvRows rows={[
            { label: "Total Agents",    value: num(ag.total_agents) },
            { label: "Total Analyses",  value: num(ag.total_analyses) },
            { label: "Total Decisions", value: num(ag.total_decisions) },
            { label: "Avg Confidence",  value: score(ag.overall_avg_confidence) },
            { label: "Avg Duration",    value: ms(ag.overall_avg_duration_ms) },
            { label: "Human Overrides", value: num(ag.total_overrides) },
          ]} />
        </SectionCard>
      </div>

      {/* ── Row 5: Top agents bar ── */}
      {topAgents.length > 0 && (
        <SectionCard title="Top Agents — Analyses vs Decisions">
          <ResponsiveContainer width="100%" height={200}>
            <BarChart data={topAgents} margin={{ top: 4, right: 8, bottom: 0, left: -20 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
              <XAxis dataKey="name" tick={{ fill: "#4b5563", fontSize: 10 }} />
              <YAxis tick={{ fill: "#4b5563", fontSize: 10 }} />
              <Tooltip {...TOOLTIP_STYLE} />
              <Legend wrapperStyle={{ fontSize: 10, color: "#9ca3af" }} />
              <Bar dataKey="analyses" name="Analyses" fill="#6366f1" radius={[3,3,0,0]} />
              <Bar dataKey="decisions" name="Decisions" fill="#22d3ee" radius={[3,3,0,0]} />
            </BarChart>
          </ResponsiveContainer>
        </SectionCard>
      )}

      {/* ── Row 6: Compliance rules pass rates ── */}
      {rulePassRates.length > 0 && (
        <SectionCard title="Compliance Rule Pass / Fail">
          <ResponsiveContainer width="100%" height={200}>
            <BarChart data={rulePassRates} margin={{ top: 4, right: 8, bottom: 0, left: -20 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
              <XAxis dataKey="name" tick={{ fill: "#4b5563", fontSize: 10 }} />
              <YAxis tick={{ fill: "#4b5563", fontSize: 10 }} />
              <Tooltip {...TOOLTIP_STYLE} />
              <Legend wrapperStyle={{ fontSize: 10, color: "#9ca3af" }} />
              <Bar dataKey="passed" name="Passed" fill="#34d399" stackId="a" />
              <Bar dataKey="failed" name="Failed" fill="#f87171" stackId="a" radius={[3,3,0,0]} />
            </BarChart>
          </ResponsiveContainer>
        </SectionCard>
      )}

      {/* ── Row 7: Compliance + Stream registry KPIs ── */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <SectionCard title="Compliance Audit">
          <KvRows rows={[
            { label: "Total Checks",        value: num(ca.total_checks) },
            { label: "Passed",              value: num(ca.passed) },
            { label: "Failed",              value: num(ca.failed) },
            { label: "Pending",             value: num(ca.pending) },
            { label: "Apps Evaluated",      value: num(ca.apps_evaluated) },
            { label: "Unique Rules",        value: num(ca.unique_rules) },
            { label: "Total Snapshots",     value: num(cs.total_snapshots) },
            { label: "Apps with Snapshots", value: num(cs.apps_with_snapshots) },
          ]} />
        </SectionCard>

        <SectionCard title="Outbox & Stream Registry">
          <KvRows rows={[
            { label: "Total Messages", value: num(ob.total_messages) },
            { label: "Published",      value: num(ob.published) },
            { label: "Pending",        value: num(ob.pending) },
            { label: "Max Attempts",   value: num(ob.max_attempts) },
            { label: "—", value: "" },
            { label: "Total Streams",  value: num(sr.total_streams) },
            { label: "Active",         value: num(sr.active_streams) },
            { label: "Archived",       value: num(sr.archived_streams) },
          ]} />
        </SectionCard>
      </div>

      {/* ── Row 8: Sector revenue bar ── */}
      {sectorRevenue.length > 0 && (
        <SectionCard title="Avg Revenue by Sector (Applicant Registry)">
          <ResponsiveContainer width="100%" height={200}>
            <BarChart data={sectorRevenue} margin={{ top: 4, right: 8, bottom: 40, left: 10 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
              <XAxis dataKey="name" tick={{ fill: "#4b5563", fontSize: 9 }} angle={-30} textAnchor="end" interval={0} />
              <YAxis tick={{ fill: "#4b5563", fontSize: 10 }} tickFormatter={(v) => "$" + (v / 1_000_000).toFixed(0) + "M"} />
              <Tooltip {...TOOLTIP_STYLE} formatter={(v: unknown) => "$" + Number(v).toLocaleString()} />
              <Bar dataKey="revenue" name="Avg Revenue" radius={[3,3,0,0]}>
                {sectorRevenue.map((_: D, i: number) => <Cell key={i} fill={PALETTE[i % PALETTE.length]} />)}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </SectionCard>
      )}

      {/* ── Row 9: Registry + Financial + Loan KPIs ── */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <SectionCard title="Applicant Registry">
          <KvRows rows={[
            { label: "Companies",      value: num(reg.total_companies) },
            { label: "Sectors",        value: num(reg.sectors) },
            { label: "Jurisdictions",  value: num(reg.jurisdictions) },
            { label: "Total Flags",    value: num(fl.total_flags) },
            { label: "Active Flags",   value: num(fl.active_flags) },
            { label: "Cos. Flagged",   value: num(fl.companies_flagged) },
          ]} />
        </SectionCard>

        <SectionCard title="Financial History">
          <KvRows rows={[
            { label: "Records",             value: num(fin.total_records) },
            { label: "Cos. w/ Financials",  value: num(fin.companies_with_financials) },
            { label: "Fiscal Years",        value: num(fin.fiscal_years) },
            { label: "Avg Revenue",         value: money(fin.avg_revenue) },
            { label: "Avg Net Income",      value: money(fin.avg_net_income) },
            { label: "Avg EBITDA Margin",   value: pct(fin.avg_ebitda_margin) },
          ]} />
        </SectionCard>

        <SectionCard title="Loan Relationships">
          <KvRows rows={[
            { label: "Total Loans",    value: num(ln.total_loans) },
            { label: "Borrowers",      value: num(ln.borrowers) },
            { label: "Avg Loan",       value: money(ln.avg_loan_usd) },
            { label: "Total Book",     value: money(ln.total_loan_usd) },
            { label: "Defaults",       value: num(ln.defaults) },
            { label: "Default Rate",   value: pct(ln.default_rate_pct) },
          ]} />
        </SectionCard>
      </div>

      {/* ── Row 10: Loan status pie ── */}
      {loanStatusDist.length > 0 && (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <SectionCard title="Loan Status Distribution">
            <ResponsiveContainer width="100%" height={200}>
              <PieChart>
                <Pie data={loanStatusDist} dataKey="value" nameKey="name" cx="50%" cy="50%" innerRadius={50} outerRadius={80} paddingAngle={3}>
                  {loanStatusDist.map((_: D, i: number) => <Cell key={i} fill={PALETTE[i % PALETTE.length]} />)}
                </Pie>
                <Tooltip {...TOOLTIP_STYLE} />
                <Legend wrapperStyle={{ fontSize: 10, color: "#9ca3af" }} />
              </PieChart>
            </ResponsiveContainer>
          </SectionCard>

          <SectionCard title="Compliance Flag Types">
            {(data.flag_type_dist ?? []).length > 0 ? (
              <ResponsiveContainer width="100%" height={200}>
                <BarChart layout="vertical" data={data.flag_type_dist} margin={{ top: 4, right: 20, bottom: 0, left: 80 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
                  <XAxis type="number" tick={{ fill: "#4b5563", fontSize: 10 }} />
                  <YAxis type="category" dataKey="flag_type" tick={{ fill: "#9ca3af", fontSize: 9 }} width={80} />
                  <Tooltip {...TOOLTIP_STYLE} />
                  <Bar dataKey="total" name="Total" fill="#a78bfa" radius={[0,3,3,0]} />
                  <Bar dataKey="active" name="Active" fill="#f87171" radius={[0,3,3,0]} />
                </BarChart>
              </ResponsiveContainer>
            ) : <p className="text-gray-600 text-xs py-8 text-center">No flag data</p>}
          </SectionCard>
        </div>
      )}

      {data.generated_at && (
        <p className="text-gray-700 text-xs text-right">
          Generated: {new Date(String(data.generated_at)).toLocaleString()}
        </p>
      )}
    </div>
  );
}
