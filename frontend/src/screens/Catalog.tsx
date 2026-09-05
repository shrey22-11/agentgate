import { Fragment, useMemo, type ReactNode } from "react";
import { api, type Verdict } from "../api";
import { Banner, Icon, Skeleton, StatTile, VerdictBadge, cx, floorAtCap, inr, useAsync } from "../ui";
import { agentBlurb } from "../components/Selectors";

/** Precedence order matches app.policy.rules — the first rule that fires
 *  decides the verdict; later rules are never consulted. */
const RULES: { id: string; desc: string }[] = [
  { id: "RULE_INPUT_INVALID", desc: "Fails closed when a request is structurally invalid or self-contradictory." },
  { id: "RULE_AGENT_ACTIVE", desc: "Only agents with ACTIVE status may perform commercial actions." },
  { id: "RULE_ACTION_PERMISSION", desc: "Blocks an action type the agent isn't explicitly permitted to perform." },
  { id: "RULE_TRANSACTION_CAP", desc: "Prevents an agent from executing transactions above its permitted per-transaction amount." },
  { id: "RULE_STOCK_AVAILABLE", desc: "Blocks purchases when the requested quantity exceeds available stock." },
  { id: "RULE_DISCOUNT_POLICY", desc: "Triggers a counter-offer when the requested discount exceeds the allowed ceiling." },
  { id: "RULE_PRICE_FLOOR", desc: "Prevents pricing below the product's configured margin floor." },
  { id: "RULE_OK", desc: "All checks passed — the request is authorised exactly as requested." },
];

const PIPELINE = [
  "REQUEST",
  "AGENT",
  "ACTION PERMISSION",
  "TRANSACTION CAP",
  "STOCK",
  "DISCOUNT / PRICE FLOOR",
  "DECISION",
];

interface Scenario {
  input: ReactNode;
  verdict: Verdict;
  note: string;
}

export function Catalog() {
  const products = useAsync(() => api.products(), []);
  const agents = useAsync(() => api.agents(), []);
  const dashboard = useAsync(() => api.dashboard(), []);

  const activeAgentCount = (agents.data ?? []).filter((a) => a.status === "ACTIVE").length;
  const referenceAgent = (agents.data ?? []).find((a) => a.type === "AI_BUYER" && a.status === "ACTIVE");
  const discountRange = useMemo(() => {
    const pcts = (products.data ?? []).map((p) => Number(p.max_discount_pct));
    return pcts.length ? { min: Math.min(...pcts), max: Math.max(...pcts) } : null;
  }, [products.data]);
  const latestPolicyVersion = dashboard.data?.recent_decisions[0]?.policy_version ?? null;

  // Worked examples computed live from the real catalogue/agent data above —
  // not hardcoded copy, so they can never drift out of sync with the seed.
  const scenarios: Scenario[] = useMemo(() => {
    const list = products.data ?? [];
    if (!list.length) return [];
    const shoe = list.find((p) => p.name.includes("Featherlite")) ?? list[0];
    const outOfStock = list.find((p) => p.stock === 0);
    const out: Scenario[] = [
      {
        input: <>1 × <b>{shoe.name}</b>, no discount requested</>,
        verdict: "ALLOW",
        note: `At list price (${inr(shoe.price)}), every check passes.`,
      },
      {
        input: <>1 × <b>{shoe.name}</b>, 50% discount requested</>,
        verdict: "COUNTER_OFFER",
        note: `Above the ${shoe.max_discount_pct}% ceiling — countered at the policy floor, ${inr(floorAtCap(shoe))}.`,
      },
    ];
    if (referenceAgent) {
      const total = Number(shoe.price) * 10;
      const overCap = total > Number(referenceAgent.max_transaction_amount);
      out.push({
        input: <>10 × <b>{shoe.name}</b> = {inr(total)}</>,
        verdict: overCap ? "NEEDS_APPROVAL" : "ALLOW",
        note: overCap
          ? `Exceeds ${referenceAgent.name}'s ${inr(referenceAgent.max_transaction_amount)} cap — routed to a human.`
          : `Within ${referenceAgent.name}'s ${inr(referenceAgent.max_transaction_amount)} cap.`,
      });
    }
    if (outOfStock) {
      out.push({
        input: <>1 × <b>{outOfStock.name}</b></>,
        verdict: "DENY",
        note: "Zero stock on hand — nothing to fulfil.",
      });
    }
    return out;
  }, [products.data, referenceAgent]);

  return (
    <div className="rise">
      <div className="page-head">
        <div>
          <h1>Products &amp; Policy</h1>
          <p>
            The commercial boundaries the deterministic engine reads — list price, stock, the
            maximum auto-approved discount, and the hard margin floor — plus how they combine into
            a decision. All <b>SIMULATED</b>.
          </p>
        </div>
      </div>

      {/* ---- A. Policy overview -------------------------------------- */}
      <div className="grid cols-4" style={{ marginBottom: 10 }}>
        {products.loading || agents.loading ? (
          [0, 1, 2, 3].map((i) => <Skeleton key={i} h={92} />)
        ) : (
          <>
            <StatTile label="Governed products" value={products.data?.length ?? 0} />
            <StatTile
              label="Active agents"
              value={activeAgentCount}
              sub={`of ${agents.data?.length ?? 0} total`}
            />
            <StatTile
              label="Reference agent cap"
              value={referenceAgent ? inr(referenceAgent.max_transaction_amount) : "—"}
              sub="per transaction, before approval"
            />
            <StatTile
              label="Discount ceilings"
              value={discountRange ? `${discountRange.min}–${discountRange.max}%` : "—"}
              sub="across the catalogue"
            />
          </>
        )}
      </div>
      {latestPolicyVersion && (
        <p className="muted" style={{ fontSize: 11.5, margin: "0 0 18px" }}>
          Current policy version <span className="kbd">{latestPolicyVersion}</span> — from the most
          recent decision.
        </p>
      )}

      {/* ---- C. Decision pipeline -------------------------------------- */}
      <div className="card card--pad" style={{ marginBottom: 18 }}>
        <div className="card-title">Decision pipeline</div>
        <div className="pipeline">
          {PIPELINE.map((step, i) => (
            <Fragment key={step}>
              <span className="pipeline__step">{step}</span>
              {i < PIPELINE.length - 1 && (
                <span className="pipeline__arrow">
                  <Icon name="arrow" size={14} />
                </span>
              )}
            </Fragment>
          ))}
        </div>
        <div className="pipeline__outcomes">
          <VerdictBadge verdict="ALLOW" />
          <VerdictBadge verdict="COUNTER_OFFER" />
          <VerdictBadge verdict="NEEDS_APPROVAL" />
          <VerdictBadge verdict="DENY" />
        </div>
        <p className="muted" style={{ fontSize: 11.5, marginTop: 14, marginBottom: 0 }}>
          The first rule that fires decides the verdict — later rules in the pipeline are never
          consulted. An AI agent can reach the top of this pipeline; only the pipeline decides what
          comes out the bottom.
        </p>
      </div>

      {/* ---- Catalogue table (existing) ------------------------------- */}
      <div className="card card--pad" style={{ marginBottom: 18 }}>
        <div className="card-title">Catalogue — {products.data?.length ?? 0} products</div>
        {products.loading ? (
          <Skeleton h={220} />
        ) : products.error ? (
          <Banner kind="err">{products.error.message}</Banner>
        ) : (
          <div className="tbl-wrap">
            <table className="tbl">
              <thead>
                <tr>
                  <th>Product</th><th>Category</th><th>List price</th><th>Stock</th>
                  <th>Max discount</th><th>Margin floor</th><th>Floor at cap</th>
                </tr>
              </thead>
              <tbody>
                {products.data!.map((p) => (
                  <tr key={p.id}>
                    <td style={{ fontWeight: 600, color: "var(--text-0)" }}>{p.name}</td>
                    <td><span className="chip">{p.category}</span></td>
                    <td className="num">{inr(p.price)}</td>
                    <td className={cx("num", p.stock === 0 && "v-deny")} style={p.stock === 0 ? { color: "var(--deny)" } : undefined}>
                      {p.stock}
                    </td>
                    <td className="num">{p.max_discount_pct}%</td>
                    <td className="num">{inr(p.min_margin_price)}</td>
                    <td className="num" style={{ color: "var(--counter)" }}>{inr(floorAtCap(p))}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="muted" style={{ fontSize: 11.5, marginTop: 12 }}>
          <b>Floor at cap</b> = <span className="kbd">max(price × (1 − max_discount), margin_floor)</span> —
          the deterministic value a COUNTER_OFFER lands on. The LLM never computes this.
        </p>
      </div>

      {/* ---- B. Deterministic rules ------------------------------------ */}
      <div className="card card--pad" style={{ marginBottom: 18 }}>
        <div className="card-title">Deterministic rules — in precedence order</div>
        <div className="rule-list">
          {RULES.map((r) => (
            <div className="rule-row" key={r.id}>
              <span className="chip chip--mono">{r.id}</span>
              <span className="rule-row__desc">{r.desc}</span>
            </div>
          ))}
        </div>
      </div>

      {/* ---- D. Policy scenarios ---------------------------------------- */}
      <div className="card card--pad" style={{ marginBottom: 18 }}>
        <div className="card-title">Policy scenarios — what happens?</div>
        {products.loading || agents.loading ? (
          <div className="stack">{[0, 1, 2].map((i) => <Skeleton key={i} h={58} />)}</div>
        ) : scenarios.length === 0 ? (
          <div className="muted" style={{ fontSize: 12.5 }}>No products to illustrate scenarios with yet.</div>
        ) : (
          <div className="stack" style={{ gap: 10 }}>
            {scenarios.map((s, i) => (
              <div className="scenario" key={i}>
                <div className="scenario__input">{s.input}</div>
                <div className="scenario__result">
                  <VerdictBadge verdict={s.verdict} />
                  <span className="scenario__note">{s.note}</span>
                </div>
              </div>
            ))}
          </div>
        )}
        <p className="muted" style={{ fontSize: 11.5, marginTop: 12, marginBottom: 0 }}>
          Illustrative — computed live from the current catalogue and reference agent, not a fixed
          backend flow. Try them yourself on the AI Buyer console.
        </p>
      </div>

      {/* ---- E. Agents (existing, enriched) ------------------------------ */}
      <div className="card card--pad">
        <div className="card-title">Agents — {agents.data?.length ?? 0}</div>
        {agents.loading ? (
          <Skeleton h={140} />
        ) : agents.error ? (
          <Banner kind="err">{agents.error.message}</Banner>
        ) : (
          <div className="grid cols-3">
            {agents.data!.map((a) => (
              <div key={a.id} className="card card--pad">
                <div className="spread">
                  <b style={{ color: "var(--text-0)", fontSize: 13.5 }}>{a.name}</b>
                  <span className={cx("pulse-dot", a.status !== "ACTIVE" && "pulse-dot--off")} />
                </div>
                <div className="row" style={{ marginTop: 10, gap: 6 }}>
                  <span className="chip">{a.type}</span>
                  <span
                    className={cx("badge", a.status === "ACTIVE" ? "v-allow" : "v-deny")}
                    style={{ fontSize: 10.5 }}
                  >
                    {a.status}
                  </span>
                </div>
                <div className="muted" style={{ fontSize: 12, marginTop: 10 }}>
                  Txn cap <b style={{ color: "var(--text-1)" }}>{inr(a.max_transaction_amount)}</b>
                </div>
                <div className="row" style={{ marginTop: 8, gap: 6 }}>
                  {a.allowed_actions.length ? (
                    a.allowed_actions.map((x) => <span key={x} className="chip chip--mono">{x}</span>)
                  ) : (
                    <span className="chip" style={{ color: "var(--deny)" }}>no actions permitted</span>
                  )}
                </div>
                <p className="muted" style={{ fontSize: 11.5, marginTop: 10, marginBottom: 0, lineHeight: 1.5 }}>
                  {agentBlurb(a)}
                </p>
              </div>
            ))}
          </div>
        )}
        <p className="muted" style={{ fontSize: 11.5, marginTop: 12 }}>
          <Icon name="info" size={12} /> This is identity + limits only — not an authentication system.
        </p>
      </div>
    </div>
  );
}
