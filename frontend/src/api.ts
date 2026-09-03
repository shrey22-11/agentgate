/* Typed client for the AgentGate backend. Relative paths — same-origin in
   production, proxied to :8000 in dev (see vite.config.ts). */

export type Verdict = "ALLOW" | "DENY" | "NEEDS_APPROVAL" | "COUNTER_OFFER";

export class ApiError extends Error {
  status: number;
  code?: string;
  constructor(status: number, message: string, code?: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, {
      ...init,
      headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    });
  } catch {
    throw new ApiError(0, "Cannot reach the AgentGate API. Is the backend running?");
  }
  const text = await res.text();
  const body = text ? safeJson(text) : null;
  if (!res.ok) {
    const detail = body?.detail;
    if (detail && typeof detail === "object") {
      throw new ApiError(res.status, detail.message ?? JSON.stringify(detail), detail.code);
    }
    if (Array.isArray(detail)) {
      throw new ApiError(res.status, detail[0]?.msg ?? "Validation error");
    }
    throw new ApiError(res.status, (typeof detail === "string" && detail) || res.statusText);
  }
  return body as T;
}
function safeJson(t: string) {
  try {
    return JSON.parse(t);
  } catch {
    return null;
  }
}
const get = <T,>(p: string) => req<T>(p);
const post = <T,>(p: string, data: unknown) =>
  req<T>(p, { method: "POST", body: JSON.stringify(data) });

/* ---- shapes ---------------------------------------------------------- */
export interface CounterOffer {
  price: string;
  discount_pct: string;
}
export interface Decision {
  action_request_id: string;
  decision_id: string;
  verdict: Verdict;
  rule_id: string;
  reason: string;
  policy_version: string;
  counter_offer: CounterOffer | null;
}
export interface NLActionResponse {
  decision: Decision;
  confidence: string;
  resolved_product: string | null;
  parse_notes: string | null;
  override_instructions_detected: boolean;
}
export interface TranscriptEntry {
  step: number;
  kind: "model_text" | "tool_call" | "tool_result";
  tool: string | null;
  detail: unknown;
}
export type BuyerOutcome =
  | "purchased"
  | "counter_offer_accepted"
  | "counter_offer_received"
  | "needs_approval"
  | "denied"
  | "no_action"
  | "budget_exhausted"
  | "ai_unavailable";
export interface BuyerRunResponse {
  goal: string;
  outcome: BuyerOutcome;
  summary: string | null;
  request_action_count: number;
  steps_used: number;
  final_decision: Decision | null;
  transcript: TranscriptEntry[];
}
export interface Product {
  id: string;
  name: string;
  description: string | null;
  category: string;
  price: string;
  stock: number;
  max_discount_pct: string;
  min_margin_price: string;
}
export interface Agent {
  id: string;
  name: string;
  type: string;
  status: "ACTIVE" | "SUSPENDED" | "DISABLED";
  max_transaction_amount: string;
  allowed_actions: string[];
}
export interface AuditEvent {
  id: string;
  seq: number;
  ref_type: string;
  ref_id: string;
  event_type: string;
  payload: Record<string, unknown>;
  prev_hash: string;
  hash: string;
  created_at: string;
}
export interface AuditChain {
  valid: boolean;
  checked_events: number;
  failure: string | null;
  failure_detail: string | null;
  event_id: string | null;
  event_seq: number | null;
}
export interface PendingApproval {
  decision_id: string;
  action_request_id: string;
  policy_version: string;
  original_rule_id: string;
  original_reason: string;
  decision_created_at: string;
  agent_id: string;
  agent_name: string;
  product_id: string;
  product_name: string;
  product_price: string;
  action_type: string;
  quantity: number | null;
  requested_discount_pct: string | null;
  proposed_price: string | null;
}
export interface ApprovalResolution {
  approval_id: string;
  decision_id: string;
  action_request_id: string;
  outcome: "APPROVED" | "REJECTED";
  approver: string;
  reason: string | null;
  resolved_at: string;
}
export interface RecentDecision {
  decision_id: string;
  action_request_id: string;
  verdict: Verdict;
  rule_id: string;
  reason: string;
  policy_version: string;
  created_at: string;
  agent_name: string | null;
  product_name: string | null;
  counter_offer_price: string | null;
}
export interface DashboardSummary {
  action_requests_total: number;
  decisions_by_verdict: Record<string, number>;
  action_requests_by_status: Record<string, number>;
  approvals_pending: number;
  approvals_resolved: Record<string, number>;
  payments_by_status: Record<string, number>;
  audit_events: number;
  audit_chain_valid: boolean;
  recent_decisions: RecentDecision[];
}

/* ---- calls -------------------------------------------------------- */
export const api = {
  health: () => get<{ status: string; database: string; environment: string }>("/health"),

  dashboard: () => get<DashboardSummary>("/dashboard/summary"),
  products: () => get<Product[]>("/catalog/products"),
  agents: () => get<Agent[]>("/catalog/agents"),

  auditEvents: (params?: { limit?: number; ref_id?: string }) => {
    const q = new URLSearchParams();
    if (params?.limit) q.set("limit", String(params.limit));
    if (params?.ref_id) q.set("ref_id", params.ref_id);
    const s = q.toString();
    return get<AuditEvent[]>("/audit/events" + (s ? `?${s}` : ""));
  },
  auditChain: () => get<AuditChain>("/audit/chain"),

  action: (body: {
    agent_id: string;
    product_id: string;
    action_type?: string;
    quantity?: number | null;
    requested_discount_pct?: string | null;
    proposed_price?: string | null;
  }) => post<Decision>("/actions", stripNulls(body)),

  aiParse: (body: { agent_id: string; text: string }) =>
    post<NLActionResponse>("/ai/actions", body),

  aiBuyer: (body: { agent_id: string; goal: string }) =>
    post<BuyerRunResponse>("/ai/buyer", body),

  pendingApprovals: () => get<PendingApproval[]>("/approvals/pending"),
  approve: (decisionId: string, body: { approver: string; reason?: string }) =>
    post<ApprovalResolution>(`/approvals/${decisionId}/approve`, stripNulls(body)),
  reject: (decisionId: string, body: { approver: string; reason?: string }) =>
    post<ApprovalResolution>(`/approvals/${decisionId}/reject`, stripNulls(body)),
};

function stripNulls<T extends Record<string, unknown>>(o: T): Partial<T> {
  return Object.fromEntries(
    Object.entries(o).filter(([, v]) => v !== null && v !== undefined && v !== ""),
  ) as Partial<T>;
}
