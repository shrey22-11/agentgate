import type { Decision, Product } from "../api";
import { inr } from "../ui";

/**
 * The itemised "what am I buying" receipt shown above the policy decision in
 * Structured mode, where the product/quantity/discount are known client-side
 * inputs. Final price / Total always come from `decision.executable_amount`
 * (the deterministic engine's output, echoed by the API) — never recomputed
 * from list price + discount here, so this can never show a number that
 * disagrees with what Pay Now will actually charge.
 */
export function PurchaseSummary({
  product,
  quantity,
  requestedDiscountPct,
  decision,
}: {
  product: Product;
  quantity: number;
  requestedDiscountPct: number;
  decision: Decision;
}) {
  const total = decision.executable_amount != null ? Number(decision.executable_amount) : null;
  const finalUnit = total != null && quantity > 0 ? total / quantity : null;

  return (
    <div className="card card--pad">
      <div className="card-title">Order summary</div>
      <div className="receipt">
        <div className="receipt__row">
          <span>Product</span>
          <span>{product.name}</span>
        </div>
        <div className="receipt__row">
          <span>Qty</span>
          <span className="num">{quantity}</span>
        </div>
        <div className="receipt__row">
          <span>List price</span>
          <span className="num">{inr(product.price)}</span>
        </div>
        <div className="receipt__row">
          <span>Requested discount</span>
          <span className="num">{requestedDiscountPct}%</span>
        </div>
        {decision.verdict === "COUNTER_OFFER" && (
          <div className="receipt__row">
            <span>Policy maximum</span>
            <span className="num">{product.max_discount_pct}%</span>
          </div>
        )}
        {finalUnit != null && (
          <div className="receipt__row">
            <span>Final price</span>
            <span className="num">{inr(finalUnit)}</span>
          </div>
        )}
        {total != null && (
          <div className="receipt__row receipt__row--total">
            <span>Total</span>
            <span className="num">{inr(total)}</span>
          </div>
        )}
      </div>
    </div>
  );
}
