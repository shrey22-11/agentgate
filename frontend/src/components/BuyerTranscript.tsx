import { useState } from "react";
import type { TranscriptEntry } from "../api";
import { VerdictBadge, cx, inr, titleCase } from "../ui";

/**
 * The detailed, developer/judge-facing agent trace — kept fully intact
 * (every tool call, argument and result the run actually produced) but
 * grouped and compacted: a tool_call is always immediately followed by its
 * matching tool_result (see app/ai/buyer.py's loop), so each pair renders as
 * ONE row with a friendly label and a couple of derived detail lines. The
 * exact raw payload for a row is still one click away via its "Raw" toggle —
 * never deleted, just no longer the default view. This component lives
 * behind AgentActivity's "View agent activity" disclosure; it never decides
 * whether to show itself.
 */

type Row =
  | { kind: "text"; step: number; text: string }
  | { kind: "pair"; step: number; tool: string; call: unknown; result: unknown }
  | { kind: "solo"; step: number; entry: TranscriptEntry };

function groupTranscript(entries: TranscriptEntry[]): Row[] {
  const rows: Row[] = [];
  for (let i = 0; i < entries.length; i++) {
    const e = entries[i];
    if (e.kind === "model_text") {
      rows.push({ kind: "text", step: e.step, text: String(e.detail) });
      continue;
    }
    if (e.kind === "tool_call") {
      const next = entries[i + 1];
      if (next && next.kind === "tool_result" && next.tool === e.tool) {
        rows.push({ kind: "pair", step: e.step, tool: e.tool ?? "tool", call: e.detail, result: next.detail });
        i++; // consumed the paired result
        continue;
      }
      rows.push({ kind: "solo", step: e.step, entry: e }); // defensive — the backend always pairs these
      continue;
    }
    rows.push({ kind: "solo", step: e.step, entry: e }); // an unmatched tool_result
  }
  return rows;
}

const TOOL_LABEL: Record<string, string> = {
  search_catalog: "Search catalogue",
  get_product: "Look up product",
  compare_products: "Compare products",
  request_action: "Request purchase",
};
const friendlyLabel = (tool: string) => TOOL_LABEL[tool] ?? titleCase(tool);

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null;
}
function str(v: unknown): string | null {
  return typeof v === "string" && v ? v : null;
}
function num(v: unknown): number | null {
  if (typeof v === "number") return v;
  if (typeof v === "string" && v.trim() !== "" && !Number.isNaN(Number(v))) return Number(v);
  return null;
}

/** Never invents a fact — every line here is read straight off the actual
 *  tool call/result JSON, never guessed or hardcoded per product/verdict. */
function describePair(tool: string, call: unknown, result: unknown): { lines: string[]; verdict: string | null; error: string | null } {
  const c = isRecord(call) ? call : {};
  const r = isRecord(result) ? result : {};
  const error = str(r.error);
  const lines: string[] = [];
  let verdict: string | null = null;

  switch (tool) {
    case "search_catalog": {
      const bits: string[] = [];
      if (str(c.query)) bits.push(`"${c.query}"`);
      if (str(c.category)) bits.push(String(c.category));
      if (str(c.max_price_inr)) bits.push(`under ${inr(c.max_price_inr as string)}`);
      if (bits.length) lines.push(bits.join(" · "));
      if (!error) {
        const count = num(r.count) ?? 0;
        lines.push(count === 1 ? "1 result" : `${count} results`);
      }
      break;
    }
    case "get_product": {
      if (!error) {
        const stock = num(r.stock);
        const bits = [
          str(r.name),
          r.price != null ? inr(r.price as string) : null,
          typeof r.in_stock === "boolean" && !r.in_stock ? "out of stock" : stock != null ? `${stock} in stock` : null,
        ].filter(Boolean) as string[];
        lines.push(bits.length ? bits.join(" · ") : "Product details retrieved");
      }
      break;
    }
    case "compare_products": {
      if (!error) {
        const products = Array.isArray(r.products) ? r.products : [];
        const names = products.map((p) => (isRecord(p) ? str(p.name) : null)).filter(Boolean) as string[];
        lines.push(names.length ? names.join(", ") : `${products.length} products compared`);
      }
      break;
    }
    case "request_action": {
      const bits: string[] = [];
      const qty = num(c.quantity);
      if (qty != null) bits.push(`Quantity: ${qty}`);
      if (str(c.requested_discount_pct)) bits.push(`Requested discount: ${c.requested_discount_pct}%`);
      if (str(c.proposed_price)) bits.push(`Proposed price: ${inr(c.proposed_price as string)}`);
      if (String(c.action_type) === "ACCEPT_COUNTER_OFFER") bits.push("Accepting AgentGate's counter-offer");
      if (bits.length) lines.push(bits.join(" · "));
      if (!error) verdict = str(r.verdict);
      break;
    }
    default:
      break; // unrecognised tool — the Raw toggle still shows exactly what happened
  }
  return { lines, verdict, error };
}

function compact(v: unknown): string {
  try {
    const s = JSON.stringify(v, null, 2);
    return s.length > 1400 ? s.slice(0, 1400) + "\n…" : s;
  } catch {
    return String(v);
  }
}

export function BuyerTranscript({ entries }: { entries: TranscriptEntry[] }) {
  const rows = groupTranscript(entries);
  const [openRaw, setOpenRaw] = useState<Set<number>>(new Set());
  const toggleRaw = (i: number) =>
    setOpenRaw((s) => {
      const next = new Set(s);
      if (next.has(i)) next.delete(i);
      else next.add(i);
      return next;
    });

  return (
    <div className="trace">
      {rows.map((row, i) => {
        if (row.kind === "text") {
          return (
            <p key={i} className="trace__note">
              {row.text}
            </p>
          );
        }

        const isPair = row.kind === "pair";
        const tool = isPair ? row.tool : row.entry.tool ?? "step";
        const { lines, verdict, error } = isPair
          ? describePair(row.tool, row.call, row.result)
          : { lines: [], verdict: null, error: null };
        const raw = isPair ? { call: row.call, result: row.result } : row.entry.detail;

        return (
          <div key={i} className="trace__row">
            <div className="trace__head">
              <span className="trace__num">{row.step}</span>
              <span className="trace__label">{friendlyLabel(tool)}</span>
              {verdict && <VerdictBadge verdict={verdict} />}
              <button type="button" className="trace__raw-toggle" onClick={() => toggleRaw(i)}>
                {openRaw.has(i) ? "Hide raw" : "Raw"}
              </button>
            </div>
            {lines.map((l, li) => (
              <div key={li} className="trace__detail">
                {l}
              </div>
            ))}
            {error && <div className={cx("trace__detail", "trace__detail--error")}>{error}</div>}
            {openRaw.has(i) && <pre className="trace__raw">{compact(raw)}</pre>}
          </div>
        );
      })}
    </div>
  );
}
