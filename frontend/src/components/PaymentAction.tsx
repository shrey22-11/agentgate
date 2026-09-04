import { useState } from "react";
import { ApiError, api, type Decision } from "../api";
import { Banner, Button, Icon, inr, useToast } from "../ui";

/**
 * The one place a customer can actually spend money from the AI Buyer
 * console. Verdict-aware:
 *
 *   ALLOW           -> Pay Now, calls POST /payments/{decision_id}/execute
 *                       and navigates the browser to the Razorpay short_url.
 *   NEEDS_APPROVAL  -> explains the cap breach, links to the Approval Queue.
 *                       Never shows Pay Now — a fresh NEEDS_APPROVAL decision
 *                       has no Approval row yet, so execute would 409.
 *   COUNTER_OFFER   -> the deterministic floor price + Accept, which
 *                       resubmits the SAME product/quantity as ACCEPT_COUNTER_OFFER
 *                       (re-evaluated by the full policy engine — defence in
 *                       depth, never trusted client-side).
 *   DENY            -> nothing here; DecisionReveal already shows the rule + reason.
 *
 * `productId`/`quantity` are only reliably known in Structured mode; when
 * absent (Natural Language without a resolved product, or Buyer Agent mode)
 * the Accept button is replaced with an explanatory note instead of guessing.
 */
export function PaymentAction({
  decision,
  agentId,
  productId,
  quantity,
  agentMaxTransaction,
  onNavigateApprovals,
  onAccepted,
}: {
  decision: Decision;
  agentId: string;
  productId: string | null;
  quantity: number | null;
  agentMaxTransaction: string | null;
  onNavigateApprovals: () => void;
  onAccepted: (next: Decision) => void;
}) {
  const [paying, setPaying] = useState(false);
  const [payErr, setPayErr] = useState<string | null>(null);
  const [accepting, setAccepting] = useState(false);
  const [acceptErr, setAcceptErr] = useState<string | null>(null);
  const toast = useToast();

  async function payNow() {
    if (paying) return; // guards against a double-click firing two payment links
    setPaying(true);
    setPayErr(null);
    try {
      const result = await api.executePayment(decision.decision_id);
      if (!result.short_url) {
        throw new ApiError(0, "Razorpay did not return a payment link. Please try again.");
      }
      try {
        sessionStorage.setItem(
          `agentgate:pay:${decision.decision_id}`,
          JSON.stringify({ amount: result.amount, currency: result.currency }),
        );
      } catch {
        /* sessionStorage unavailable (private mode etc.) — purely cosmetic on the
           result page, safe to proceed without it */
      }
      window.location.href = result.short_url; // real Razorpay-hosted page — a full navigation, not a route change
    } catch (e) {
      const ae = e instanceof ApiError ? e : new ApiError(0, String(e));
      setPayErr(ae.message);
      setPaying(false);
    }
  }

  async function acceptCounterOffer() {
    if (accepting || !decision.counter_offer || !productId) return;
    setAccepting(true);
    setAcceptErr(null);
    try {
      const next = await api.action({
        agent_id: agentId,
        product_id: productId,
        action_type: "ACCEPT_COUNTER_OFFER",
        quantity: quantity ?? 1,
        proposed_price: decision.counter_offer.price,
      });
      onAccepted(next);
      toast(
        next.verdict === "ALLOW" ? "ok" : "err",
        `Counter-offer response: ${next.verdict.replace(/_/g, " ")}`,
      );
    } catch (e) {
      const ae = e instanceof ApiError ? e : new ApiError(0, String(e));
      setAcceptErr(ae.message);
    } finally {
      setAccepting(false);
    }
  }

  if (decision.verdict === "ALLOW") {
    if (decision.executable_amount == null) return null;
    return (
      <div className="stack">
        {payErr && <Banner kind="err">{payErr}</Banner>}
        <div className="pay-bar">
          <div className="pay-bar__amount">
            <span>Amount due</span>
            <b>{inr(decision.executable_amount)}</b>
          </div>
          <Button onClick={payNow} loading={paying} className="btn--lg">
            {paying ? (
              "Creating secure payment…"
            ) : (
              <>
                <Icon name="shield" size={15} /> Pay Now {inr(decision.executable_amount)}
              </>
            )}
          </Button>
        </div>
      </div>
    );
  }

  if (decision.verdict === "NEEDS_APPROVAL") {
    return (
      <div className="approval-block">
        <p className="muted" style={{ fontSize: 12.5, margin: 0, lineHeight: 1.5 }}>
          This transaction exceeds the agent's per-transaction limit and needs a human decision
          in the Approval Queue before it can be paid.
        </p>
        <div className="approval-block__amounts">
          <div>
            <span>Requested amount</span>
            <b className="over">{inr(decision.executable_amount)}</b>
          </div>
          <div>
            <span>Agent limit</span>
            <b>{inr(agentMaxTransaction)}</b>
          </div>
        </div>
        <Button variant="ghost" onClick={onNavigateApprovals}>
          View approval status <Icon name="arrow" size={14} />
        </Button>
      </div>
    );
  }

  if (decision.verdict === "COUNTER_OFFER" && decision.counter_offer) {
    return (
      <div className="stack">
        {acceptErr && <Banner kind="err">{acceptErr}</Banner>}
        <div className="negotiate__offer">
          <div>
            <span className="muted" style={{ fontSize: 11.5, display: "block", marginBottom: 4 }}>
              AgentGate counter-offer
            </span>
            <b>{inr(decision.counter_offer.price)}</b>
          </div>
          {productId ? (
            <Button onClick={acceptCounterOffer} loading={accepting}>
              <Icon name="check" size={14} /> Accept counter-offer
            </Button>
          ) : (
            <span className="muted" style={{ fontSize: 11.5, maxWidth: 220 }}>
              Not resubmittable from this mode — ask again in Structured mode to accept.
            </span>
          )}
        </div>
      </div>
    );
  }

  return null;
}
