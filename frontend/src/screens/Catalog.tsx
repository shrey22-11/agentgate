import { api } from "../api";
import { Banner, Icon, Skeleton, cx, inr, useAsync } from "../ui";

export function Catalog() {
  const products = useAsync(() => api.products(), []);
  const agents = useAsync(() => api.agents(), []);

  return (
    <div className="rise">
      <div className="page-head">
        <div>
          <h1>Products &amp; Policy</h1>
          <p>
            The commercial boundaries the deterministic engine reads: list price, stock,
            the maximum auto-approved discount, and the hard margin floor. All <b>SIMULATED</b>.
          </p>
        </div>
      </div>

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
                {products.data!.map((p) => {
                  const atCap = Number(p.price) * (1 - Number(p.max_discount_pct) / 100);
                  const floor = Math.max(atCap, Number(p.min_margin_price));
                  return (
                    <tr key={p.id}>
                      <td style={{ fontWeight: 600, color: "var(--text-0)" }}>{p.name}</td>
                      <td><span className="chip">{p.category}</span></td>
                      <td className="num">{inr(p.price)}</td>
                      <td className={cx("num", p.stock === 0 && "v-deny")} style={p.stock === 0 ? { color: "var(--deny)" } : undefined}>
                        {p.stock}
                      </td>
                      <td className="num">{p.max_discount_pct}%</td>
                      <td className="num">{inr(p.min_margin_price)}</td>
                      <td className="num" style={{ color: "var(--counter)" }}>{inr(floor)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        <p className="muted" style={{ fontSize: 11.5, marginTop: 12 }}>
          <b>Floor at cap</b> = <span className="kbd">max(price × (1 − max_discount), margin_floor)</span> —
          the deterministic value a COUNTER_OFFER lands on. The LLM never computes this.
        </p>
      </div>

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
