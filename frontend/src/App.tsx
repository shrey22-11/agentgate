import { useEffect, useState } from "react";
import "./styles.css";
import { api } from "./api";
import { Icon, ToastProvider, cx } from "./ui";
import { BuyerConsole } from "./screens/BuyerConsole";
import { AuditTimeline } from "./screens/AuditTimeline";
import { Dashboard } from "./screens/Dashboard";
import { Approvals } from "./screens/Approvals";
import { Catalog } from "./screens/Catalog";

type View = "buyer" | "audit" | "dashboard" | "approvals" | "catalog";

const NAV: { id: View; label: string; icon: string }[] = [
  { id: "buyer", label: "AI Buyer", icon: "bot" },
  { id: "audit", label: "Audit Timeline", icon: "timeline" },
  { id: "dashboard", label: "Dashboard", icon: "grid" },
  { id: "approvals", label: "Approval Queue", icon: "gavel" },
  { id: "catalog", label: "Products & Policy", icon: "tag" },
];

export default function App() {
  const [view, setView] = useState<View>("buyer");
  const [pending, setPending] = useState(0);
  const [env, setEnv] = useState<string | null>(null);
  const [online, setOnline] = useState<boolean | null>(null);

  useEffect(() => {
    const tick = () => {
      api.health().then((h) => { setOnline(true); setEnv(h.environment); }).catch(() => setOnline(false));
      api.pendingApprovals().then((p) => setPending(p.length)).catch(() => {});
    };
    tick();
    const t = setInterval(tick, 15000);
    return () => clearInterval(t);
  }, [view]);

  return (
    <ToastProvider>
      <div className="app-bg" />
      <div className="shell">
        <aside className="sidebar">
          <div className="brand">
            <div className="brand__mark">AG</div>
            <div>
              <div className="brand__name">AgentGate</div>
              <div className="brand__tag">decision &amp; control layer</div>
            </div>
          </div>

          {NAV.map((n) => (
            <button
              key={n.id}
              className="nav-item"
              aria-current={view === n.id}
              onClick={() => setView(n.id)}
            >
              <span className="nav-item__ico"><Icon name={n.icon} size={18} /></span>
              <span>{n.label}</span>
              {n.id === "approvals" && pending > 0 && (
                <span className="nav-item__badge">{pending}</span>
              )}
            </button>
          ))}

          <div className="sidebar__foot">
            <div className="row" style={{ gap: 8, marginBottom: 6 }}>
              <span className={cx("pulse-dot", !online && "pulse-dot--off")} />
              <span>{online === null ? "connecting…" : online ? `API online · ${env}` : "API unreachable"}</span>
            </div>
            Every money action explainable, bounded &amp; gated.
          </div>
        </aside>

        <main className="main">
          {view === "buyer" && <BuyerConsole />}
          {view === "audit" && <AuditTimeline />}
          {view === "dashboard" && <Dashboard onNavigate={setView} />}
          {view === "approvals" && <Approvals onResolved={() => setPending((p) => Math.max(0, p - 1))} />}
          {view === "catalog" && <Catalog />}
        </main>
      </div>
    </ToastProvider>
  );
}
