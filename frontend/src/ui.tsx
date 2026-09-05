import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ButtonHTMLAttributes,
  type CSSProperties,
  type ReactNode,
} from "react";
import { ApiError, type Product, type Verdict } from "./api";

export const cx = (...c: (string | false | null | undefined)[]) => c.filter(Boolean).join(" ");

/* ---- formatting -------------------------------------------------------- */
export function inr(v: string | number | null | undefined): string {
  if (v === null || v === undefined || v === "") return "—";
  const n = typeof v === "string" ? Number(v) : v;
  if (Number.isNaN(n)) return String(v);
  return "₹" + n.toLocaleString("en-IN", { maximumFractionDigits: 2 });
}
/**
 * The lowest unit price a COUNTER_OFFER could ever land on for this product —
 * display only, mirrors `app.counter_offer.compute_floor`'s formula
 * (max(list price at the discount cap, margin floor)) over already-public
 * catalogue fields. Never used to gate, decide, or submit anything: the real
 * figure always comes back on `decision.counter_offer.price` from the
 * deterministic engine.
 */
export function floorAtCap(p: Pick<Product, "price" | "max_discount_pct" | "min_margin_price">): number {
  const atCap = Number(p.price) * (1 - Number(p.max_discount_pct) / 100);
  return Math.max(atCap, Number(p.min_margin_price));
}
export const shortHash = (h: string, n = 10) =>
  !h ? "" : h === "0".repeat(64) ? "GENESIS" : h.slice(0, n) + "…";
export function relTime(iso: string): string {
  const diff = (Date.now() - new Date(iso).getTime()) / 1000;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return new Date(iso).toLocaleDateString();
}
export const titleCase = (s: string) =>
  s.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

/* ---- primitives ----------------------------------------------------- */
export function Button({
  children,
  variant = "primary",
  size,
  loading,
  className,
  ...rest
}: {
  children: ReactNode;
  variant?: "primary" | "ghost" | "danger";
  size?: "sm";
  loading?: boolean;
} & ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      {...rest}
      disabled={rest.disabled || loading}
      className={cx(
        "btn",
        variant === "ghost" && "btn--ghost",
        variant === "danger" && "btn--danger",
        size === "sm" && "btn--sm",
        className,
      )}
    >
      {loading && <span className="spinner" />}
      {children}
    </button>
  );
}

export function Field({
  label,
  children,
  hint,
}: {
  label: string;
  children: ReactNode;
  hint?: ReactNode;
}) {
  return (
    <div className="field">
      <label>{label}</label>
      {children}
      {hint && <span className="muted" style={{ fontSize: 11.5 }}>{hint}</span>}
    </div>
  );
}

export function Spinner() {
  return <span className="spinner" />;
}

/** A stock-aware +/- quantity control. Display/UX only — the backend
 *  (`RULE_STOCK_AVAILABLE`) remains the sole authority on whether a quantity
 *  is actually fulfillable; `max` here just keeps the control from inviting an
 *  obviously-doomed request. */
export function Stepper({
  value,
  onChange,
  min = 1,
  max,
}: {
  value: number;
  onChange: (n: number) => void;
  min?: number;
  max: number;
}) {
  return (
    <div className="stepper">
      <button
        type="button"
        onClick={() => onChange(Math.max(min, value - 1))}
        disabled={value <= min}
        aria-label="Decrease quantity"
      >
        −
      </button>
      <span className="stepper__value">{value}</span>
      <button
        type="button"
        onClick={() => onChange(Math.min(max, value + 1))}
        disabled={value >= max}
        aria-label="Increase quantity"
      >
        +
      </button>
    </div>
  );
}

export function Skeleton({ h = 16, w = "100%", style }: { h?: number; w?: number | string; style?: CSSProperties }) {
  return <div className="skeleton" style={{ height: h, width: w, ...style }} />;
}

export function VerdictBadge({ verdict }: { verdict: Verdict | string }) {
  return (
    <span className={cx("badge", `v-${verdict.toLowerCase()}`)}>
      <span className="badge__dot" />
      {verdict.replace(/_/g, " ")}
    </span>
  );
}

export function EmptyState({ title, hint, icon }: { title: string; hint?: string; icon?: ReactNode }) {
  return (
    <div className="empty">
      {icon ?? <Icon name="inbox" size={30} />}
      <div style={{ fontWeight: 650, color: "var(--text-2)", marginTop: 8 }}>{title}</div>
      {hint && <div style={{ fontSize: 12.5, marginTop: 4 }}>{hint}</div>}
    </div>
  );
}

export function Banner({ kind, children }: { kind: "warn" | "err" | "info"; children: ReactNode }) {
  return (
    <div className={cx("banner", `banner--${kind}`)}>
      <Icon name={kind === "err" ? "alert" : kind === "warn" ? "warn" : "info"} size={16} />
      <div>{children}</div>
    </div>
  );
}

/* ---- count-up stat ------------------------------------------------- */
export function StatTile({
  label,
  value,
  sub,
}: {
  label: string;
  value: number | string;
  sub?: ReactNode;
}) {
  const numeric = typeof value === "number";
  const shown = useCountUp(numeric ? value : 0, 700);
  return (
    <div className="stat">
      <div className="stat__label">{label}</div>
      <div className="stat__value">{numeric ? shown : value}</div>
      {sub && <div className="stat__sub">{sub}</div>}
    </div>
  );
}
export function useCountUp(target: number, ms = 600): number {
  const [n, setN] = useState(0);
  const from = useRef(0);
  useEffect(() => {
    const start = performance.now();
    const a = from.current;
    let raf = 0;
    const tick = (t: number) => {
      const p = Math.min(1, (t - start) / ms);
      const eased = 1 - Math.pow(1 - p, 3);
      setN(Math.round(a + (target - a) * eased));
      if (p < 1) raf = requestAnimationFrame(tick);
      else from.current = target;
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [target, ms]);
  return n;
}

/* ---- data hooks ------------------------------------------------- */
export function useAsync<T>(fn: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(true);
  const run = useCallback(() => {
    setLoading(true);
    setError(null);
    fn()
      .then((d) => setData(d))
      .catch((e) => setError(e instanceof ApiError ? e : new ApiError(0, String(e))))
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  useEffect(() => {
    run();
  }, [run]);
  return { data, error, loading, refetch: run };
}

/* ---- toasts -------------------------------------------------- */
type Toast = { id: number; kind: "ok" | "err"; msg: string };
const ToastCtx = createContext<(kind: "ok" | "err", msg: string) => void>(() => {});
export const useToast = () => useContext(ToastCtx);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const push = useCallback((kind: "ok" | "err", msg: string) => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t, { id, kind, msg }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 4200);
  }, []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="toasts">
        {toasts.map((t) => (
          <div key={t.id} className={cx("toast", `toast--${t.kind}`)}>
            <Icon name={t.kind === "ok" ? "check" : "alert"} size={15} />
            <div>{t.msg}</div>
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}

/* ---- icons (inline, no dep) --------------------------------- */
const PATHS: Record<string, ReactNode> = {
  bot: <><rect x="4" y="8" width="16" height="12" rx="3" /><path d="M12 8V4M9 4h6M9 14h.01M15 14h.01M9 17h6" /></>,
  timeline: <><path d="M6 3v18M6 7h13M6 12h9M6 17h13" /><circle cx="6" cy="7" r="1.6" fill="currentColor" stroke="none" /><circle cx="6" cy="12" r="1.6" fill="currentColor" stroke="none" /><circle cx="6" cy="17" r="1.6" fill="currentColor" stroke="none" /></>,
  grid: <><rect x="3" y="3" width="8" height="8" rx="2" /><rect x="13" y="3" width="8" height="8" rx="2" /><rect x="3" y="13" width="8" height="8" rx="2" /><rect x="13" y="13" width="8" height="8" rx="2" /></>,
  gavel: <><path d="M14 4l6 6M12 6l6 6M8.5 9.5l6 6M3 21h8M5 19l6.5-6.5" /></>,
  tag: <><path d="M20.6 13.4L13 21l-9-9 7.6-7.6a2 2 0 011.4-.6H19a2 2 0 012 2v6a2 2 0 01-.4 1.6z" /><circle cx="16" cy="8" r="1.5" fill="currentColor" stroke="none" /></>,
  inbox: <><path d="M4 13h4l2 3h4l2-3h4M4 13V6a2 2 0 012-2h12a2 2 0 012 2v7M4 13v5a2 2 0 002 2h12a2 2 0 002-2v-5" /></>,
  check: <path d="M4 12l5 5L20 6" />,
  alert: <><path d="M12 9v4M12 17h.01" /><circle cx="12" cy="12" r="9" /></>,
  warn: <><path d="M12 3l10 18H2L12 3zM12 10v4M12 18h.01" /></>,
  info: <><circle cx="12" cy="12" r="9" /><path d="M12 11v6M12 8h.01" /></>,
  shield: <><path d="M12 3l8 3v6c0 5-3.4 8-8 9-4.6-1-8-4-8-9V6l8-3z" /><path d="M9 12l2 2 4-4" /></>,
  arrow: <path d="M5 12h14M13 6l6 6-6 6" />,
  send: <path d="M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z" />,
  refresh: <><path d="M21 12a9 9 0 11-3-6.7M21 3v6h-6" /></>,
  spark: <path d="M12 2l2.4 6.6L21 11l-6.6 2.4L12 20l-2.4-6.6L3 11l6.6-2.4L12 2z" />,
  clock: <><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3.5 2" /></>,
  close: <><circle cx="12" cy="12" r="9" /><path d="M9.5 9.5l5 5M14.5 9.5l-5 5" /></>,
  search: <><circle cx="11" cy="11" r="7" /><path d="M21 21l-4.3-4.3" /></>,
  swap: <><path d="M4 8h13M13 4l4 4-4 4" /><path d="M20 16H7M11 12l-4 4 4 4" /></>,
};
export function Icon({ name, size = 18 }: { name: keyof typeof PATHS | string; size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      {PATHS[name] ?? PATHS.spark}
    </svg>
  );
}
