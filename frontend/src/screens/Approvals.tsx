import { useState } from "react";
import { ApiError, api, type PendingApproval } from "../api";
import { Banner, Button, Field, Icon, Skeleton, inr, relTime, useAsync, useToast } from "../ui";

export function Approvals({ onResolved }: { onResolved: () => void }) {
  const q = useAsync(() => api.pendingApprovals(), []);
  const [approver, setApprover] = useState("ops@merchant.example");

  return (
    <div className="rise">
      <div className="page-head">
        <div>
          <h1>Approval Queue</h1>
          <p>
            <span className="badge v-needs_approval"><span className="badge__dot" />NEEDS_APPROVAL</span> decisions
            awaiting a human. Approving authorises <i>that</i> transaction as already evaluated — it never widens
            a discount, a floor, or a cap.
          </p>
        </div>
        <Button variant="ghost" size="sm" onClick={q.refetch}>
          <Icon name="refresh" size={14} /> Refresh
        </Button>
      </div>

      <div className="card card--pad" style={{ marginBottom: 18 }}>
        <Field label="Resolver identity" hint="No auth system — this is an explicit validated field.">
          <input className="input" value={approver} onChange={(e) => setApprover(e.target.value)} style={{ maxWidth: 340 }} />
        </Field>
      </div>

      {q.loading ? (
        <div className="stack">{[0, 1].map((i) => <Skeleton key={i} h={150} />)}</div>
      ) : q.error ? (
        <Banner kind="err">{q.error.message}</Banner>
      ) : (q.data?.length ?? 0) === 0 ? (
        <div className="card card--pad">
          <div className="empty">
            <Icon name="check" size={30} />
            <div style={{ fontWeight: 650, color: "var(--text-2)", marginTop: 8 }}>Queue is clear</div>
            <div style={{ fontSize: 12.5 }}>No decisions are waiting on a human.</div>
          </div>
        </div>
      ) : (
        <div className="stack">
          {q.data!.map((item) => (
            <ApprovalCard
              key={item.decision_id}
              item={item}
              approver={approver}
              onDone={() => { onResolved(); q.refetch(); }}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function ApprovalCard({
  item,
  approver,
  onDone,
}: {
  item: PendingApproval;
  approver: string;
  onDone: () => void;
}) {
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState<"" | "approve" | "reject">("");
  const [err, setErr] = useState<string | null>(null);
  const toast = useToast();

  async function resolve(kind: "approve" | "reject") {
    if (!approver.trim()) {
      setErr("Enter a resolver identity above.");
      return;
    }
    setBusy(kind);
    setErr(null);
    try {
      const body = { approver: approver.trim(), reason: reason.trim() || undefined };
      const r = kind === "approve"
        ? await api.approve(item.decision_id, body)
        : await api.reject(item.decision_id, body);
      toast("ok", `Decision ${r.outcome.toLowerCase()} by ${r.approver}`);
      onDone();
    } catch (e) {
      const m = e instanceof ApiError ? e.message : String(e);
      setErr(m);
      toast("err", m);
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="card card--pad stack">
      <div className="spread" style={{ alignItems: "flex-start" }}>
        <div>
          <div className="row" style={{ gap: 8 }}>
            <span className="badge v-needs_approval"><span className="badge__dot" />{item.original_rule_id}</span>
            <span className="chip">{item.action_type}</span>
            <span className="muted" style={{ fontSize: 11.5 }}>{relTime(item.decision_created_at)}</span>
          </div>
          <p style={{ margin: "10px 0 0", fontSize: 13, color: "var(--text-1)", maxWidth: "70ch" }}>
            {item.original_reason}
          </p>
        </div>
        <span className="chip chip--mono">policy {item.policy_version}</span>
      </div>

      <div className="row" style={{ gap: 8 }}>
        <span className="chip">agent: {item.agent_name}</span>
        <span className="chip">product: {item.product_name}</span>
        <span className="chip">list {inr(item.product_price)}</span>
        {item.quantity != null && <span className="chip">qty {item.quantity}</span>}
        {item.requested_discount_pct && <span className="chip">asked {item.requested_discount_pct}% off</span>}
        {item.proposed_price && <span className="chip">offered {inr(item.proposed_price)}</span>}
      </div>

      <input
        className="input"
        placeholder="Reason (optional)"
        value={reason}
        onChange={(e) => setReason(e.target.value)}
      />
      {err && <Banner kind="err">{err}</Banner>}
      <div className="row">
        <Button loading={busy === "approve"} disabled={!!busy} onClick={() => resolve("approve")}>
          <Icon name="check" size={14} /> Approve
        </Button>
        <Button variant="danger" loading={busy === "reject"} disabled={!!busy} onClick={() => resolve("reject")}>
          Reject
        </Button>
      </div>
    </div>
  );
}
