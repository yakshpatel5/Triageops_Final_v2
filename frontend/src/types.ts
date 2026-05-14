// View names used in the SPA router
export type View = 'dashboard' | 'alerts' | 'rules' | 'analysis' | 'settings';

// Real API response shapes — matches ops_schemas.py
export interface AlertSummary {
  id: string;
  alert_id: string;
  host: string;
  source: string;
  severity: string;
  message: string;
  received_at: string;
  triage_decision: string | null;
  confidence_score: string | null;
}

export interface SuppressionRule {
  id: string;
  tenant_id: string;
  match_field: string;
  pattern_type: string;
  host_pattern: string | null;
  message_pattern: string | null;
  alert_id_prefix: string | null;
  reason: string | null;
  is_active: string;
  hit_count: string | null;
  expires_at: string | null;
}

export interface TenantStats {
  alerts_24h: number;
  critical_24h: number;
  noise_24h: number;
  needs_review_24h: number;
  pending_approvals: number;
  escalations_24h: number;
  total_alerts: number;
  total_escalations: number;
  avg_confidence_24h: number | null;
  avg_latency_ms_24h: number | null;
  noise_ratio_24h: number | null;
}

// Legacy types kept for compatibility
export type Alert = AlertSummary;
export type Rule = SuppressionRule;
