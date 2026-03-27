"use client";

interface Props {
  stats: Record<string, number>;
  lags: Record<string, number>;
  health: Record<string, unknown>;
}

function Kpi({ label, value, color = "text-white" }: { label: string; value: string | number; color?: string }) {
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg px-4 py-3 flex flex-col gap-0.5">
      <span className="text-gray-600 text-xs tracking-widest uppercase">{label}</span>
      <span className={`text-2xl font-bold ${color}`}>{value}</span>
    </div>
  );
}

export default function StatsBar({ stats, lags, health }: Props) {
  const maxLag = Math.max(0, ...Object.values(lags));
  const dbOk = health.db === true;
  const daemonOk = health.daemon === true;

  return (
    <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-8 gap-3 mb-6">
      <Kpi label="Total" value={(stats.total ?? 0).toLocaleString()} />
      <Kpi label="Approved" value={(stats.approved ?? 0).toLocaleString()} color="text-green-400" />
      <Kpi label="Declined" value={(stats.declined ?? 0).toLocaleString()} color="text-red-400" />
      <Kpi label="Referred" value={(stats.referred ?? 0).toLocaleString()} color="text-yellow-400" />
      <Kpi label="Compliance Blocked" value={(stats.compliance_blocked ?? 0).toLocaleString()} color="text-orange-400" />
      <Kpi label="High Fraud" value={(stats.high_fraud ?? 0).toLocaleString()} color="text-red-500" />
      <Kpi label="Avg Fraud" value={stats.avg_fraud_score != null ? Number(stats.avg_fraud_score).toFixed(3) : "—"} color="text-cyan-400" />
      <div className="bg-gray-900 border border-gray-800 rounded-lg px-4 py-3 flex flex-col gap-0.5">
        <span className="text-gray-600 text-xs tracking-widest uppercase">System</span>
        <div className="flex gap-2 mt-0.5">
          <span className={`text-xs px-1.5 py-0.5 rounded ${dbOk ? "bg-green-900 text-green-400" : "bg-red-900 text-red-400"}`}>
            DB {dbOk ? "✓" : "✗"}
          </span>
          <span className={`text-xs px-1.5 py-0.5 rounded ${daemonOk ? "bg-green-900 text-green-400" : "bg-red-900 text-red-400"}`}>
            Daemon {daemonOk ? "✓" : "✗"}
          </span>
          {maxLag > 0 && (
            <span className="text-xs px-1.5 py-0.5 rounded bg-yellow-900 text-yellow-400">
              Lag {maxLag}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
