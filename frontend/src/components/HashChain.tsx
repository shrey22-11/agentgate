import { useMemo, useState } from "react";
import type { AuditEvent } from "../api";
import { relTime, shortHash } from "../ui";

const GENESIS = "0".repeat(64);

/** Renders audit events oldest→newest with visible prev_hash → hash linkage. */
export function HashChain({ events }: { events: AuditEvent[] }) {
  const ordered = useMemo(() => [...events].sort((a, b) => a.seq - b.seq), [events]);
  return (
    <div className="chain">
      {ordered.map((e, i) => {
        const prevOk =
          i === 0 ? e.prev_hash === GENESIS : e.prev_hash === ordered[i - 1].hash;
        return <ChainNode key={e.id} e={e} first={i === 0} prevOk={prevOk} />;
      })}
    </div>
  );
}

function ChainNode({ e, first, prevOk }: { e: AuditEvent; first: boolean; prevOk: boolean }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="chain__node" style={{ animationDelay: `${Math.min(e.seq % 12, 12) * 0.03}s` }}>
      <div className="chain__row">
        <span className="chain__type">{e.event_type}</span>
        <span className="chip chip--mono">#{e.seq}</span>
        <span className="chip">{e.ref_type}</span>
        <span className="muted" style={{ fontSize: 11.5, marginLeft: "auto" }}>
          {relTime(e.created_at)}
        </span>
      </div>
      <div className="chain__link">
        <span>prev</span>
        <span className={prevOk ? "ok" : "bad"}>{first ? "GENESIS" : shortHash(e.prev_hash)}</span>
        <span>→</span>
        <span>hash</span>
        <span className="chain__hash">{shortHash(e.hash, 16)}</span>
        <span className={prevOk ? "ok" : "bad"} style={{ marginLeft: 4 }}>
          {prevOk ? "linked ✓" : "BROKEN ✕"}
        </span>
      </div>
      <button
        className="chip"
        style={{ marginTop: 8, cursor: "pointer" }}
        onClick={() => setOpen((o) => !o)}
      >
        {open ? "hide payload" : "payload"}
      </button>
      {open && <pre className="chain__payload">{JSON.stringify(e.payload, null, 2)}</pre>}
    </div>
  );
}
