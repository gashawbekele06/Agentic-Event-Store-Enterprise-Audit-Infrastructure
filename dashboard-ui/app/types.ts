export interface Application {
  application_id: string;
  state: string;
  applicant_id?: string;
  requested_amount_usd?: number;
  approved_amount_usd?: number;
  risk_tier?: string;
  fraud_score?: number;
  compliance_status?: string;
  decision?: string;
  created_at?: string;
  updated_at?: string;
  last_event_type?: string;
  last_event_at?: string;
}

export interface Event {
  stream_id?: string;
  stream_position: number;
  global_position: number;
  event_type: string;
  event_version: number;
  payload: Record<string, unknown>;
  recorded_at?: string;
}

export interface Agent {
  agent_id: string;
  model_version: string;
  analyses_completed: number;
  decisions_generated: number;
  avg_confidence_score?: number;
  avg_duration_ms?: number;
  approve_count: number;
  decline_count: number;
  refer_count: number;
  human_override_count: number;
  approve_pct?: number;
  decline_pct?: number;
  refer_pct?: number;
  first_seen_at?: string;
  last_seen_at?: string;
}

export interface ComplianceCheck {
  application_id: string;
  rule_id: string;
  rule_version?: string;
  regulation_set_version?: string;
  status: string;
  failure_reason?: string;
  remediation_required?: boolean;
  evidence_hash?: string;
  evaluation_timestamp?: string;
}

export interface Stats {
  total: number;
  approved: number;
  declined: number;
  referred: number;
  compliance_blocked: number;
  high_fraud: number;
  avg_fraud_score?: number;
}

export interface Lag {
  [projection: string]: number;
}

export interface GlobalEvent extends Event {
  stream_id: string;
}

export type TabId =
  | "home"
  | "applications"
  | "compliance"
  | "agents"
  | "events"
  | "pipeline"
  | "demo"
  | "global";
