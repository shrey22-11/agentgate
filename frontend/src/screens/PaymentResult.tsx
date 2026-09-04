import { useEffect, useRef, useState } from "react";
import { ApiError, api, type PaymentExecution } from "../api";
import { Button, Icon, Spinner, inr } from "../ui";

/**
 * The customer's post-checkout landing page. Razorpay's `callback_url`
 * (see app.razorpay.service._callback_url) brings the browser back here with
 * `?payment_callback=1&decision_id=...` plus a handful of `razorpay_*` query
 * params Razorpay appends itself.
 *
 * SECURITY: those `razorpay_*` params are NEVER read. A callback query string
 * is just a browser redirect — anyone can hand-craft one claiming
 * `razorpay_payment_link_status=paid`. The only thing trusted here is our own
 * `decision_id`, used solely to ask the backend (whose view of the world is
 * driven by the verified webhook, not by this page) what actually happened.
 */

const POLL_MS = 3000;
const MAX_POLLS = 20; // ~60s of automatic polling before asking the customer to check manually

type Phase = "loading" | "ready" | "not_found" | "error";

export function PaymentResult({ decisionId, onDone }: { decisionId: string; onDone: () => void }) {
  const [phase, setPhase] = useState<Phase>("loading");
  const [status, setStatus] = useState<PaymentExecution | null>(null);
  const [errMsg, setErrMsg] = useState<string | null>(null);
  const [attempts, setAttempts] = useState(0);
  const [checkingAgain, setCheckingAgain] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  async function fetchOnce(): Promise<PaymentExecution | null> {
    try {
      const r = await api.paymentStatus(decisionId);
      setStatus(r);
      setPhase("ready");
      setErrMsg(null);
      return r;
    } catch (e) {
      const ae = e instanceof ApiError ? e : new ApiError(0, String(e));
      if (ae.status === 404 || ae.status === 409) {
        setPhase("not_found");
      } else {
        setPhase("error");
        setErrMsg(ae.message);
      }
      return null;
    }
  }

  useEffect(() => {
    let cancelled = false;
    let n = 0;

    async function tick() {
      const r = await fetchOnce();
      n += 1;
      if (cancelled) return;
      setAttempts(n);
      const stillPending = r && (r.status === "CREATED" || r.status === "PENDING");
      if (stillPending && n < MAX_POLLS) {
        timer.current = setTimeout(tick, POLL_MS);
      }
    }
    tick();

    return () => {
      cancelled = true;
      if (timer.current) clearTimeout(timer.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [decisionId]);

  async function checkAgain() {
    if (timer.current) clearTimeout(timer.current); // don't stack a second polling chain
    setCheckingAgain(true);
    const r = await fetchOnce();
    setCheckingAgain(false);
    setAttempts(0);
    if (r && (r.status === "CREATED" || r.status === "PENDING")) {
      timer.current = setTimeout(function loop() {
        fetchOnce().then((r2) => {
          if (r2 && (r2.status === "CREATED" || r2.status === "PENDING")) {
            timer.current = setTimeout(loop, POLL_MS);
          }
        });
      }, POLL_MS);
    }
  }

  return (
    <div className="result-page">
      <div className="result-page__brand">
        <div className="brand__mark">AG</div>
        <span>AgentGate</span>
      </div>

      <div className="result-card">
        {phase === "loading" && (
          <>
            <div className="status-icon status-icon--info">
              <Spinner />
            </div>
            <h1>Checking your payment…</h1>
            <p className="result-card__sub">One moment — we're confirming this with AgentGate.</p>
          </>
        )}

        {phase === "not_found" && (
          <>
            <div className="status-icon status-icon--info">
              <Icon name="info" size={28} />
            </div>
            <h1>We couldn't find this payment</h1>
            <p className="result-card__sub">
              This link may be stale, or the payment hasn't been created yet. If you were just
              redirected from Razorpay, try checking again in a moment.
            </p>
            <div className="row" style={{ justifyContent: "center", marginTop: 8 }}>
              <Button variant="ghost" loading={checkingAgain} onClick={checkAgain}>
                <Icon name="refresh" size={14} /> Check again
              </Button>
            </div>
          </>
        )}

        {phase === "error" && (
          <>
            <div className="status-icon status-icon--warn">
              <Icon name="warn" size={28} />
            </div>
            <h1>Couldn't reach AgentGate</h1>
            <p className="result-card__sub">{errMsg ?? "The status check failed. Your payment is unaffected."}</p>
            <div className="row" style={{ justifyContent: "center", marginTop: 8 }}>
              <Button variant="ghost" loading={checkingAgain} onClick={checkAgain}>
                <Icon name="refresh" size={14} /> Try again
              </Button>
            </div>
          </>
        )}

        {phase === "ready" && status && <ResultBody status={status} attempts={attempts} onCheckAgain={checkAgain} checking={checkingAgain} />}

        <div className="hr" style={{ margin: "24px 0 18px" }} />
        <Button variant="ghost" className="btn--block" onClick={onDone}>
          <Icon name="bot" size={14} /> Back to AI Buyer
        </Button>
        <p className="result-card__foot">
          Payment status is confirmed by AgentGate's backend via the Razorpay webhook — never by
          this page alone.
        </p>
      </div>
    </div>
  );
}

function ResultBody({
  status,
  attempts,
  onCheckAgain,
  checking,
}: {
  status: PaymentExecution;
  attempts: number;
  onCheckAgain: () => void;
  checking: boolean;
}) {
  if (status.status === "PAID") {
    return (
      <>
        <div className="status-icon status-icon--ok">
          <Icon name="check" size={30} />
        </div>
        <h1>Payment successful</h1>
        <p className="result-card__sub">Your payment has been confirmed.</p>
        <div className="result-card__amount">{inr(status.amount)}</div>
        {status.razorpay_payment_link_id && (
          <div className="result-card__meta">
            <div className="result-card__meta-row">
              <span>Reference</span>
              <span>{status.razorpay_payment_link_id}</span>
            </div>
          </div>
        )}
      </>
    );
  }

  if (status.status === "CREATED" || status.status === "PENDING") {
    const stalled = attempts >= MAX_POLLS;
    return (
      <>
        <div className="status-icon status-icon--info">
          <Icon name="clock" size={28} />
        </div>
        <h1>Confirming your payment…</h1>
        <p className="result-card__sub">
          {stalled
            ? "This is taking longer than expected. Your payment may still be processing on Razorpay's side — check again in a moment."
            : "We've received your payment and are waiting for Razorpay to confirm it. This usually takes a few seconds."}
        </p>
        <div className="result-card__amount">{inr(status.amount)}</div>
        <div className="row" style={{ justifyContent: "center" }}>
          {stalled ? (
            <Button variant="ghost" loading={checking} onClick={onCheckAgain}>
              <Icon name="refresh" size={14} /> Check again
            </Button>
          ) : (
            <span className="muted" style={{ fontSize: 12, display: "inline-flex", alignItems: "center", gap: 8 }}>
              <Spinner /> Checking automatically…
            </span>
          )}
        </div>
      </>
    );
  }

  if (status.status === "EXPIRED") {
    return (
      <>
        <div className="status-icon status-icon--warn">
          <Icon name="warn" size={28} />
        </div>
        <h1>Payment link expired</h1>
        <p className="result-card__sub">
          This payment link expired before it was completed. No charge was made — start a new
          purchase to try again.
        </p>
        <div className="result-card__amount">{inr(status.amount)}</div>
      </>
    );
  }

  // FAILED
  return (
    <>
      <div className="status-icon status-icon--bad">
        <Icon name="close" size={28} />
      </div>
      <h1>Payment not completed</h1>
      <p className="result-card__sub">
        This payment wasn't completed and nothing was charged. Start a new purchase to try again.
      </p>
      <div className="result-card__amount">{inr(status.amount)}</div>
    </>
  );
}
