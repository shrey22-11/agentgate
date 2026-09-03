import { api } from "../api";
import { Banner, Button, Icon, Skeleton, cx, useAsync } from "../ui";
import { HashChain } from "../components/HashChain";

export function AuditTimeline() {
  const events = useAsync(() => api.auditEvents({ limit: 300 }), []);
  const chain = useAsync(() => api.auditChain(), []);

  function refresh() {
    events.refetch();
    chain.refetch();
  }

  return (
    <div className="rise">
      <div className="page-head">
        <div>
          <h1>Audit Timeline</h1>
          <p>
            Append-only, hash-chained. Each event's <span className="kbd">hash</span> is SHA-256 over
            its fields plus the previous event's hash. The database rejects UPDATE / DELETE / TRUNCATE.
          </p>
        </div>
        <Button variant="ghost" size="sm" onClick={refresh}>
          <Icon name="refresh" size={14} /> Re-verify
        </Button>
      </div>

      <div className="grid cols-3" style={{ marginBottom: 20 }}>
        <div className="card card--pad">
          <div className="card-title">Chain integrity</div>
          {chain.loading ? (
            <Skeleton h={40} />
          ) : chain.error ? (
            <Banner kind="err">{chain.error.message}</Banner>
          ) : (
            <div className="row" style={{ gap: 10 }}>
              <span className={cx("pulse-dot", !chain.data?.valid && "pulse-dot--off")} />
              <span
                className={cx("badge", chain.data?.valid ? "v-allow" : "v-deny")}
                style={{ fontSize: 13 }}
              >
                {chain.data?.valid ? "VALID" : chain.data?.failure ?? "INVALID"}
              </span>
            </div>
          )}
        </div>
        <div className="card card--pad">
          <div className="card-title">Events verified</div>
          <div className="stat__value" style={{ fontSize: 26 }}>
            {chain.loading ? "…" : chain.data?.checked_events ?? 0}
          </div>
        </div>
        <div className="card card--pad">
          <div className="card-title">Tamper detection</div>
          <div style={{ fontSize: 12.5, color: "var(--text-2)", lineHeight: 1.5 }}>
            {chain.data?.failure_detail ??
              "Payload, hash, prev-hash, reorder, and mid-chain deletion are all detected."}
          </div>
        </div>
      </div>

      <div className="card card--pad">
        <div className="card-title">Events — newest first, {events.data?.length ?? 0} shown</div>
        {events.loading ? (
          <div className="stack">{[0, 1, 2, 3, 4].map((i) => <Skeleton key={i} h={72} />)}</div>
        ) : events.error ? (
          <Banner kind="err">{events.error.message}</Banner>
        ) : (events.data?.length ?? 0) === 0 ? (
          <div className="empty">
            <Icon name="timeline" size={30} />
            <div style={{ fontWeight: 650, color: "var(--text-2)", marginTop: 8 }}>No events yet</div>
            <div style={{ fontSize: 12.5 }}>
              Submit a request on the AI Buyer screen — every decision writes to the chain.
            </div>
          </div>
        ) : (
          <HashChain events={events.data!} />
        )}
      </div>
    </div>
  );
}
