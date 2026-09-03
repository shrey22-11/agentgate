import { api } from "../api";
import {
  Banner,
  Button,
  Icon,
  Skeleton,
  StatTile,
  VerdictBadge,
  cx,
  inr,
  relTime,
  useAsync,
} from "../ui";

const VERDICTS = ["ALLOW", "DENY", "NEEDS_APPROVAL", "COUNTER_OFFER"] as const;

export function Dashboard({ onNavigate }: { onNavigate: (v: "audit" | "approvals" | "catalog") => void }) {
  const s = useAsync(() => api.dashboard(), []);

  if (s.loading)
    return (
      <div className="rise">
        <div className="page-head"><div><h1>Merchant Dashboard</h1></div></div>
        <div className="grid cols-4">{[0, 1, 2, 3].map((i) => <Skeleton key={i} h={104} />)}</div>
      </div>
    );
  if (s.error || !s.data)
    return (
      <div className="rise">
        <div className="page-head"><div><h1>Merchant Dashboard</h1></div></div>
        <Banner kind="err">{s.error?.message ?? "No data"}</Banner>
      </div>
    );

  const d = s.data;
  const totalDecisions = VERDICTS.reduce((n, v) => n + (d.decisions_by_verdict[v] ?? 0), 0);

  return (
    <div className="rise">
      <div className="page-head">
        <div>
          <h1>Merchant Dashboard</h1>
          <p>
            Counts of AgentGate's decisions over a <b>SIMULATED</b> catalogue and agent population.
            No revenue or conversion figures — those aren't claimed.
          </p>
        </div>
        <Button variant="ghost" size="sm" onClick={s.refetch}>
          <Icon name="refresh" size={14} /> Refresh
        </Button>
      </div>

      <div className="grid cols-4" style={{ marginBottom: 18 }}>
        <StatTile label="Action requests" value={d.action_requests_total} />
        <StatTile
          label="Decisions"
          value={totalDecisions}
          sub={`${d.decisions_by_verdict.ALLOW ?? 0} allowed`}
        />
        <StatTile
          label="Pending approvals"
          value={d.approvals_pending}
          sub={d.approvals_pending > 0 ? "needs a human" : "queue clear"}
        />
        <StatTile
          label="Audit events"
          value={d.audit_events}
          sub={
            <span className={cx("badge", d.audit_chain_valid ? "v-allow" : "v-deny")} style={{ fontSize: 10.5 }}>
              chain {d.audit_chain_valid ? "valid" : "BROKEN"}
            </span>
          }
        />
      </div>

      <div className="grid cols-2" style={{ alignItems: "start", marginBottom: 18 }}>
        <div className="card card--pad">
          <div className="card-title">Verdict mix</div>
          <div className="stack" style={{ gap: 12 }}>
            {VERDICTS.map((v) => {
              const n = d.decisions_by_verdict[v] ?? 0;
              const pct = totalDecisions ? Math.round((n / totalDecisions) * 100) : 0;
              return (
                <div key={v}>
                  <div className="spread" style={{ marginBottom: 6 }}>
                    <VerdictBadge verdict={v} />
                    <span className="num">{n} · {pct}%</span>
                  </div>
                  <div style={{ height: 8, borderRadius: 999, background: "rgba(255,255,255,0.05)", overflow: "hidden" }}>
                    <div
                      style={{
                        height: "100%",
                        width: `${pct}%`,
                        borderRadius: 999,
                        transition: "width .6s cubic-bezier(.2,.9,.3,1)",
                        background:
                          v === "ALLOW" ? "var(--allow)" :
                          v === "DENY" ? "var(--deny)" :
                          v === "NEEDS_APPROVAL" ? "var(--approval)" : "var(--counter)",
                      }}
                    />
                  </div>
                </div>
              );
            })}
          </div>
        </div>

        <div className="card card--pad">
          <div className="card-title">Payments (Razorpay test-mode)</div>
          {Object.keys(d.payments_by_status).length === 0 ? (
            <div className="muted" style={{ fontSize: 12.5 }}>
              No payment attempts yet. Execution runs off an ALLOW decision via
              <span className="kbd">POST /payments/&#123;id&#125;/execute</span>.
            </div>
          ) : (
            <div className="row">
              {Object.entries(d.payments_by_status).map(([k, n]) => (
                <span key={k} className="chip">{k}: {n}</span>
              ))}
            </div>
          )}
          <hr className="hr" style={{ margin: "16px 0" }} />
          <div className="card-title">Approvals resolved</div>
          <div className="row">
            <span className="chip">approved: {d.approvals_resolved.APPROVED ?? 0}</span>
            <span className="chip">rejected: {d.approvals_resolved.REJECTED ?? 0}</span>
            {d.approvals_pending > 0 && (
              <Button size="sm" variant="ghost" onClick={() => onNavigate("approvals")}>
                Review {d.approvals_pending} <Icon name="arrow" size={13} />
              </Button>
            )}
          </div>
        </div>
      </div>

      <div className="card card--pad">
        <div className="spread" style={{ marginBottom: 6 }}>
          <div className="card-title" style={{ margin: 0 }}>Recent decisions</div>
          <Button size="sm" variant="ghost" onClick={() => onNavigate("audit")}>
            Open audit timeline <Icon name="arrow" size={13} />
          </Button>
        </div>
        {d.recent_decisions.length === 0 ? (
          <div className="empty"><Icon name="grid" size={28} /><div style={{ marginTop: 8 }}>Nothing decided yet.</div></div>
        ) : (
          <div className="tbl-wrap">
            <table className="tbl">
              <thead>
                <tr><th>When</th><th>Verdict</th><th>Rule</th><th>Agent</th><th>Product</th><th>Reason</th></tr>
              </thead>
              <tbody>
                {d.recent_decisions.map((r) => (
                  <tr key={r.decision_id}>
                    <td className="muted">{relTime(r.created_at)}</td>
                    <td><VerdictBadge verdict={r.verdict} /></td>
                    <td className="chip chip--mono" style={{ border: 0, background: "transparent" }}>{r.rule_id}</td>
                    <td>{r.agent_name ?? "—"}</td>
                    <td>{r.product_name ?? "—"}{r.counter_offer_price ? ` → ${inr(r.counter_offer_price)}` : ""}</td>
                    <td style={{ maxWidth: 380, color: "var(--text-2)" }}>{r.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
