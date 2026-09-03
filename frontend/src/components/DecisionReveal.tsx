import type { Decision } from "../api";
import { cx, inr } from "../ui";

/** The hero "AgentGate Decision" card. Colour + glow keyed to the verdict.
 *  Give it a changing React `key` from the caller to re-trigger the reveal. */
export function DecisionReveal({ decision }: { decision: Decision }) {
  const v = decision.verdict;
  return (
    <div className={cx("decision", `d-${v.toLowerCase()}`)}>
      <div className="decision__glow" />
      <div style={{ position: "relative" }}>
        <div className="decision__verdict">
          {v.replace(/_/g, " ")}
          <span className="chip chip--mono">{decision.rule_id}</span>
        </div>
        <p className="decision__reason">{decision.reason}</p>

        {decision.counter_offer && (
          <div className="decision__counter">
            <span className="muted" style={{ fontSize: 12 }}>Counter-offer</span>
            <b>{inr(decision.counter_offer.price)}</b>
            <span className="chip">{decision.counter_offer.discount_pct}% off list</span>
            <span className="muted" style={{ fontSize: 11.5 }}>
              computed by the deterministic engine — not the LLM
            </span>
          </div>
        )}

        <div className="decision__meta">
          <span className="chip chip--mono">policy {decision.policy_version}</span>
          <span className="chip chip--mono">decision {decision.decision_id.slice(0, 8)}</span>
          <span className="chip chip--mono">request {decision.action_request_id.slice(0, 8)}</span>
        </div>
      </div>
    </div>
  );
}
