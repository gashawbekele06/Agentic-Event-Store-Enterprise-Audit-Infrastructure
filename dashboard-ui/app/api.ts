const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

async function get<T>(path: string, params?: Record<string, string | number | undefined>): Promise<T> {
  const url = new URL(`${BASE}${path}`);
  if (params) {
    Object.entries(params).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, String(v));
    });
  }
  const res = await fetch(url.toString(), { cache: "no-store" });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

async function post<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
    cache: "no-store",
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

export const api = {
  health: () => get<Record<string, unknown>>("/health"),
  stats: () => get<Record<string, number>>("/api/stats"),
  lag: () => get<Record<string, number>>("/api/lag"),

  applications: (state?: string) =>
    get<Record<string, unknown>[]>("/api/applications", state ? { state } : undefined),
  application: (id: string) => get<Record<string, unknown>>(`/api/applications/${id}`),
  applicationEvents: (id: string) => get<Record<string, unknown>[]>(`/api/applications/${id}/events`),
  applicationCompliance: (id: string) => get<Record<string, unknown>[]>(`/api/applications/${id}/compliance`),

  agents: () => get<Record<string, unknown>[]>("/api/agents"),

  stream: (id: string, limit = 30, offset = 0) =>
    get<{ stream_id: string; total: number; events: Record<string, unknown>[] }>(
      `/api/stream/${id}`, { limit, offset }
    ),

  globalEvents: (limit = 50, offset = 0, event_type?: string, stream_prefix?: string) =>
    get<{ total: number; limit: number; offset: number; events: Record<string, unknown>[] }>(
      "/api/events/global", { limit, offset, event_type, stream_prefix }
    ),
  globalEventTypes: () => get<string[]>("/api/events/global/types"),

  analysis: () => get<Record<string, unknown>>("/api/analysis"),

  checkpoints: () => get<{
    projection_name: string;
    last_position: number;
    latest_global: number;
    lag_events: number;
    updated_at: string | null;
  }[]>("/api/checkpoints"),

  listDocuments: (company_id: string) =>
    get<{ name: string; size: number; ext: string }[]>(`/api/documents/${company_id}`),

  uploadDocument: async (company_id: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    const res = await fetch(`${BASE}/api/documents/${company_id}/upload`, {
      method: "POST",
      body: form,
    });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    return res.json() as Promise<{ saved: string; size: number }>;
  },

  deleteDocument: async (company_id: string, filename: string) => {
    const r = await fetch(`${BASE}/api/documents/${company_id}/${encodeURIComponent(filename)}`, { method: "DELETE" });
    if (!r.ok) throw new Error(`${r.status}`);
    return r.json();
  },

  runPipeline: (company_id: string, amount: number, app_id?: string) =>
    post<{ job_id: string; app_id: string }>("/api/pipeline/run", { company_id, amount, app_id }),
  pipelineStatus: (job_id: string) => get<Record<string, unknown>>(`/api/pipeline/status/${job_id}`),

  runDemo: (step: string) =>
    post<{ job_id: string; step: string; title: string }>(`/api/demo/run/${step}`),
  demoStatus: (job_id: string) => get<Record<string, unknown>>(`/api/demo/status/${job_id}`),
};
