import { useMemo, useState } from "react";
import {
  ApiError,
  api,
  type BuyerRunResponse,
  type Decision,
  type NLActionResponse,
} from "../api";
import {
  Banner,
  Button,
  Field,
  Icon,
  Skeleton,
  cx,
  inr,
  titleCase,
  useAsync,
  useToast,
} from "../ui";
import { DecisionReveal } from "../components/DecisionReveal";
import { BuyerTranscript } from "../components/BuyerTranscript";

type Mode = "manual" | "parse" | "agent";

const OUTCOME_TONE: Record<string, "ok" | "warn" | "bad" | "info"> = {
  purchased: "ok",
  counter_offer_accepted: "ok",
  counter_offer_received: "info",
  needs_approval: "warn",
  denied: "bad",
  no_action: "info",
  budget_exhausted: "warn",
  ai_unavailable: "bad",
};

export function BuyerConsole() {
  const meta = useAsync(async () => {
    const [agents, products] = await Promise.all([api.agents(), api.products()]);
    return { agents, products };
  }, []);

  const [mode, setMode] = useState<Mode>("manual");
  const [agentId, setAgentId] = useState("");
  const [productId, setProductId] = useState("");
  const [qty, setQty] = useState(1);
  const [discount, setDiscount] = useState(0);
  const [text, setText] = useState("I'd like the Velocity Pro Marathon Racer — can I get 20% off?");
  const [goal, setGoal] = useState("Find road-running shoes under ₹8,000 and buy a pair.");

  const [busy, setBusy] = useState(false);
  const [decision, setDecision] = useState<Decision | null>(null);
  const [parse, setParse] = useState<NLActionResponse | null>(null);
  const [run, setRun] = useState<BuyerRunResponse | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const [nonce, setNonce] = useState(0);
  const toast = useToast();

  const agents = meta.data?.agents ?? [];
  const products = meta.data?.products ?? [];
  const activeAgent = useMemo(
    () => agents.find((a) => a.id === agentId) ?? agents[0],
    [agents, agentId],
  );
  const effectiveAgent = agentId || agents[0]?.id || "";
  const effectiveProduct = productId || products[0]?.id || "";

  async function submit() {
    setBusy(true);
    setErr(null);
    setDecision(null);
    setParse(null);
    setRun(null);
    try {
      if (mode === "manual") {
        const d = await api.action({
          agent_id: effectiveAgent,
          product_id: effectiveProduct,
          quantity: qty,
          requested_discount_pct: discount > 0 ? String(discount) : null,
        });
        setDecision(d);
        flash(d.verdict);
      } else if (mode === "parse") {
        const r = await api.aiParse({ agent_id: effectiveAgent, text });
        setParse(r);
        setDecision(r.decision);
        flash(r.decision.verdict);
      } else {
        const r = await api.aiBuyer({ agent_id: effectiveAgent, goal });
        setRun(r);
        if (r.final_decision) setDecision(r.final_decision);
        toast(
          OUTCOME_TONE[r.outcome] === "bad" ? "err" : "ok",
          `Agent finished: ${titleCase(r.outcome)}`,
        );
      }
      setNonce((n) => n + 1);
    } catch (e) {
      const ae = e instanceof ApiError ? e : new ApiError(0, String(e));
      setErr(ae);
      if (ae.code !== "AI_DISABLED") toast("err", ae.message);
    } finally {
      setBusy(false);
    }
  }
  function flash(v: string) {
    toast(v === "DENY" ? "err" : "ok", `Verdict: ${v.replace(/_/g, " ")}`);
  }

  const aiDisabled = err?.code === "AI_DISABLED";

  return (
    <div className="rise">
      <div className="page-head">
        <div>
          <h1>AI Buyer Console</h1>
          <p>
            An external AI buyer proposes; AgentGate's deterministic policy decides. The
            LLM can phrase a request but never a verdict, a discount ceiling, or a payment.
          </p>
        </div>
        <div className="seg" role="tablist">
          {(["manual", "parse", "agent"] as Mode[]).map((m) => (
            <button
              key={m}
              aria-pressed={mode === m}
              onClick={() => { setMode(m); setErr(null); }}
            >
              {m === "manual" ? "Structured" : m === "parse" ? "Natural language" : "Buyer agent"}
            </button>
          ))}
        </div>
      </div>

      <div className="grid cols-2" style={{ alignItems: "start" }}>
        {/* ---- request builder ---- */}
        <div className="card card--pad stack">
          <div className="card-title">Request</div>

          {meta.loading ? (
            <>
              <Skeleton h={38} /><Skeleton h={38} /><Skeleton h={38} />
            </>
          ) : meta.error ? (
            <Banner kind="err">Couldn't load agents/products: {meta.error.message}</Banner>
          ) : (
            <>
              <Field label="Acting as agent" hint={
                activeAgent
                  ? `${activeAgent.status} · cap ${inr(activeAgent.max_transaction_amount)} · ${activeAgent.allowed_actions.join(", ") || "no actions"}`
                  : undefined
              }>
                <select className="select" value={effectiveAgent} onChange={(e) => setAgentId(e.target.value)}>
                  {agents.map((a) => (
                    <option key={a.id} value={a.id}>
                      {a.name} — {a.type}
                    </option>
                  ))}
                </select>
              </Field>

              {mode === "manual" && (
                <>
                  <Field label="Product">
                    <select className="select" value={effectiveProduct} onChange={(e) => setProductId(e.target.value)}>
                      {products.map((p) => (
                        <option key={p.id} value={p.id}>
                          {p.name} — {inr(p.price)} · {p.stock} in stock
                        </option>
                      ))}
                    </select>
                  </Field>
                  <div className="grid cols-2">
                    <Field label={`Quantity — ${qty}`}>
                      <input className="range" type="range" min={1} max={20} value={qty}
                        onChange={(e) => setQty(Number(e.target.value))} />
                    </Field>
                    <Field label={`Requested discount — ${discount}%`}>
                      <input className="range" type="range" min={0} max={80} value={discount}
                        onChange={(e) => setDiscount(Number(e.target.value))} />
                    </Field>
                  </div>
                </>
              )}

              {mode === "parse" && (
                <Field label="Natural-language request" hint="Untrusted text. Try a prompt-injection.">
                  <textarea className="textarea" value={text} onChange={(e) => setText(e.target.value)} rows={4} />
                </Field>
              )}

              {mode === "agent" && (
                <Field label="Shopping goal" hint="The agent searches, compares, and requests — bounded by a step budget.">
                  <textarea className="textarea" value={goal} onChange={(e) => setGoal(e.target.value)} rows={4} />
                </Field>
              )}

              <Button onClick={submit} loading={busy} className="btn--block">
                <Icon name={mode === "agent" ? "spark" : "send"} size={15} />
                {mode === "manual" ? "Submit to AgentGate" : mode === "parse" ? "Parse & evaluate" : "Run buyer agent"}
              </Button>

              {mode !== "manual" && (
                <p className="muted" style={{ fontSize: 11.5, margin: 0 }}>
                  Needs <span className="kbd">AI_ENABLED=true</span> + an Anthropic key. Structured
                  mode works offline and exercises the same policy engine.
                </p>
              )}
            </>
          )}
        </div>

        {/* ---- decision / transcript ---- */}
        <div className="stack">
          {aiDisabled && (
            <Banner kind="warn">
              AI is disabled on this deployment (<span className="kbd">AI_ENABLED=false</span>).
              Switch to <b>Structured</b> mode to see the deterministic decision flow now.
            </Banner>
          )}
          {err && !aiDisabled && <Banner kind="err">{err.message}</Banner>}

          {busy && (
            <div className="card card--pad stack">
              <Skeleton h={26} w="40%" />
              <Skeleton h={16} /><Skeleton h={16} w="80%" />
            </div>
          )}

          {!busy && decision && <DecisionReveal key={nonce} decision={decision} />}

          {parse && (
            <div className="card card--pad stack">
              <div className="card-title">Parse boundary</div>
              <div className="row">
                <span className="chip">confidence {parse.confidence}</span>
                <span className="chip">product: {parse.resolved_product ?? "—"}</span>
                {parse.override_instructions_detected && (
                  <span className="badge v-deny"><span className="badge__dot" />manipulation flagged</span>
                )}
              </div>
              {parse.parse_notes && <p className="muted" style={{ fontSize: 12.5, margin: 0 }}>“{parse.parse_notes}”</p>}
              {parse.override_instructions_detected && (
                <Banner kind="info">
                  The parser recorded the injection attempt but did not obey it — the request still
                  went through the normal deterministic policy path.
                </Banner>
              )}
            </div>
          )}

          {run && (
            <div className="card card--pad stack">
              <div className="spread">
                <div className="card-title" style={{ margin: 0 }}>Agent run</div>
                <span className={cx("badge", toneClass(run.outcome))}>
                  <span className="badge__dot" />{titleCase(run.outcome)}
                </span>
              </div>
              <div className="row">
                <span className="chip">{run.steps_used} model steps</span>
                <span className="chip">{run.request_action_count} request_action calls</span>
              </div>
              {run.summary && <p style={{ fontSize: 13, margin: 0, color: "var(--text-1)" }}>{run.summary}</p>}
              <hr className="hr" />
              <BuyerTranscript entries={run.transcript} />
            </div>
          )}

          {!busy && !decision && !run && !err && (
            <div className="card card--pad">
              <div className="empty" style={{ padding: "34px 10px" }}>
                <Icon name="shield" size={30} />
                <div style={{ fontWeight: 650, color: "var(--text-2)", marginTop: 8 }}>
                  No decision yet
                </div>
                <div style={{ fontSize: 12.5 }}>Build a request and submit it to AgentGate.</div>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function toneClass(outcome: string) {
  const t = OUTCOME_TONE[outcome];
  return t === "ok" ? "v-allow" : t === "bad" ? "v-deny" : t === "warn" ? "v-needs_approval" : "v-counter_offer";
}
