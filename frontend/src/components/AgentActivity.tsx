import { useState } from "react";
import type { BuyerRunResponse, TranscriptEntry, Verdict } from "../api";
import { Banner, Icon, cx, titleCase } from "../ui";
import { VERDICT_ICON } from "./DecisionReveal";
import { BuyerTranscript } from "./BuyerTranscript";

/**
 * The customer-facing face of a Buyer Agent run: a short, plain-language
 * checklist of what happened, plus the full tool-call trace collapsed by
 * default behind a "View agent activity" disclosure.
 *
 * This never re-decides or re-explains the policy outcome — `decision.reason`
 * and `decision.rule_id`, already shown by <DecisionReveal> above this
 * component, remain the one authoritative "why". The checklist below is a
 * *visual* progress trail derived purely from `run.final_decision.rule_id`
 * (the deterministic engine's own fixed precedence — reaching RULE_OK means
 * every earlier rule already passed) and from tool results that actually
 * appear in `run.transcript`. Nothing here is hardcoded per product,
 * quantity or verdict; an unrecognised rule id falls back to the real
 * verdict rather than guessing a reason.
 */

type Tone = "pass" | "allow" | "deny" | "approval" | "counter" | "neutral";
interface ActivityStep {
  label: string;
  tone: Tone;
}

const TONE_ICON: Record<Tone, string> = {
  pass: "check",
  allow: VERDICT_ICON.ALLOW,
  deny: VERDICT_ICON.DENY,
  approval: VERDICT_ICON.NEEDS_APPROVAL,
  counter: VERDICT_ICON.COUNTER_OFFER,
  neutral: "info",
};

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null;
}

/** Only claims a search "found something" when a real result count says so —
 *  a 0-result attempt still appears, faithfully, in the expandable trace. */
function foundSomethingBySearching(entries: TranscriptEntry[]): boolean {
  return entries.some((e) => {
    if (e.kind !== "tool_result" || e.tool !== "search_catalog" || !isRecord(e.detail)) return false;
    const count = e.detail.count;
    return typeof count === "number" && count > 0;
  });
}
function comparedProducts(entries: TranscriptEntry[]): boolean {
  return entries.some(
    (e) => e.kind === "tool_result" && e.tool === "compare_products" && isRecord(e.detail) && Array.isArray(e.detail.products),
  );
}

function stepsForDecision(ruleId: string, verdict: Verdict): ActivityStep[] {
  switch (ruleId) {
    case "RULE_OK":
      return [
        { label: "Availability verified", tone: "pass" },
        { label: "Policy checked", tone: "pass" },
        { label: "Purchase approved", tone: "allow" },
      ];
    case "RULE_DISCOUNT_POLICY":
    case "RULE_PRICE_FLOOR":
      return [
        { label: "Availability verified", tone: "pass" },
        { label: "Discount checked", tone: "pass" },
        { label: "Counter-offer calculated", tone: "counter" },
      ];
    case "RULE_STOCK_AVAILABLE":
      return [
        { label: "Availability checked", tone: "pass" },
        { label: "Purchase blocked", tone: "deny" },
      ];
    case "RULE_TRANSACTION_CAP":
      return [
        { label: "Transaction limit checked", tone: "pass" },
        { label: "Human approval required", tone: "approval" },
      ];
    case "RULE_ACTION_PERMISSION":
      return [
        { label: "Permission checked", tone: "pass" },
        { label: "Purchase blocked", tone: "deny" },
      ];
    case "RULE_AGENT_ACTIVE":
      return [
        { label: "Agent status checked", tone: "pass" },
        { label: "Purchase blocked", tone: "deny" },
      ];
    case "RULE_INPUT_INVALID":
      return [{ label: "Request blocked", tone: "deny" }];
    default: {
      // Forward-compatible fallback for a rule id this component doesn't
      // recognise yet — reflect the real verdict, never invent a reason.
      const tone: Tone =
        verdict === "ALLOW" ? "allow" : verdict === "DENY" ? "deny" : verdict === "NEEDS_APPROVAL" ? "approval" : "counter";
      return [{ label: titleCase(verdict), tone }];
    }
  }
}

function buildChecklist(run: BuyerRunResponse): ActivityStep[] {
  const steps: ActivityStep[] = [];
  if (foundSomethingBySearching(run.transcript)) steps.push({ label: "Searched the catalogue", tone: "pass" });
  if (comparedProducts(run.transcript)) steps.push({ label: "Compared options", tone: "pass" });

  const d = run.final_decision;
  if (!d) return steps; // no decision reached — caller renders a notice instead

  steps.push({ label: "Product found", tone: "pass" });
  const tail = stepsForDecision(d.rule_id, d.verdict);
  if (run.outcome === "counter_offer_accepted" && tail.length) {
    tail[tail.length - 1] = { label: "Counter-offer accepted", tone: "allow" };
  }
  return [...steps, ...tail];
}

// The only three BuyerOutcome values that can occur with no final_decision
// at all (see app/ai/buyer.py::_outcome) — every other outcome always comes
// with a decision, and is summarised via the checklist above instead.
const NO_DECISION_NOTICE: Partial<Record<BuyerRunResponse["outcome"], { kind: "err" | "warn" | "info"; text: string }>> = {
  ai_unavailable: { kind: "err", text: "Agent couldn't complete the request. Please try again." },
  budget_exhausted: { kind: "warn", text: "The agent reached its step limit before completing a purchase." },
  no_action: { kind: "info", text: "The agent didn't request a purchase." },
};

export function AgentActivity({ run }: { run: BuyerRunResponse }) {
  const [open, setOpen] = useState(false);
  const notice = !run.final_decision ? NO_DECISION_NOTICE[run.outcome] : undefined;
  const checklist = buildChecklist(run);
  const hasTrace = run.transcript.length > 0;

  return (
    <div className="card card--pad stack">
      <div className="card-title" style={{ margin: 0 }}>Agent activity</div>

      {notice && <Banner kind={notice.kind}>{notice.text}</Banner>}

      {checklist.length > 0 && (
        <div className="agent-activity__list">
          {checklist.map((step, i) => (
            <div key={i} className={cx("agent-activity__item", `agent-activity__item--${step.tone}`)}>
              <Icon name={TONE_ICON[step.tone]} size={16} />
              {step.label}
            </div>
          ))}
        </div>
      )}

      {run.summary && <p className="agent-activity__note">“{run.summary}”</p>}

      {hasTrace && (
        <>
          <button type="button" className="disclosure" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
            <span className={cx("disclosure__chevron", open && "disclosure__chevron--open")}>
              <Icon name="arrow" size={13} />
            </span>
            {open ? "Hide agent activity" : "View agent activity"}
          </button>
          {open && (
            <div className="disclosure__panel">
              <div className="disclosure__meta">
                <span className="chip">{run.steps_used} model steps</span>
                <span className="chip">{run.request_action_count} request_action calls</span>
                <span className="chip">{titleCase(run.outcome)}</span>
              </div>
              <BuyerTranscript entries={run.transcript} />
            </div>
          )}
        </>
      )}
    </div>
  );
}
