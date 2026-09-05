import { useEffect, useMemo, useRef, useState, type RefObject } from "react";
import type { Agent, Product } from "../api";
import { Icon, cx, inr } from "../ui";

/**
 * Custom interactive replacements for the native <select> dropdowns on the AI
 * Buyer console. Purely presentational: both still just call `onChange(id)`
 * with a real id from `products`/`agents` (as fetched from `api.products()` /
 * `api.agents()`) — nothing here invents data or changes what gets submitted.
 */

function useOutsideClick(ref: RefObject<HTMLElement>, onOutside: () => void) {
  useEffect(() => {
    function handle(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) onOutside();
    }
    document.addEventListener("mousedown", handle);
    return () => document.removeEventListener("mousedown", handle);
  }, [ref, onOutside]);
}

/** One-line, rule-based (not hardcoded per agent) explanation of what an
 *  agent can actually do — derived from its real status/allowed_actions. */
export function agentBlurb(a: Agent): string {
  if (a.status !== "ACTIVE") return "Requests are blocked while this agent isn't active.";
  if (a.allowed_actions.length === 0) return "Can browse the catalogue but cannot execute a purchase.";
  return "Can propose and execute purchases within policy.";
}

/* ---- Product selector -------------------------------------------------- */
export function ProductSelect({
  products,
  value,
  onChange,
}: {
  products: Product[];
  value: string;
  onChange: (id: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [highlight, setHighlight] = useState(0);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);

  const selected = products.find((p) => p.id === value) ?? null;
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return products;
    return products.filter(
      (p) => p.name.toLowerCase().includes(q) || p.category.toLowerCase().includes(q),
    );
  }, [products, query]);

  useOutsideClick(rootRef, () => setOpen(false));

  function openMenu() {
    setOpen(true);
    setQuery("");
    setHighlight(Math.max(0, products.findIndex((p) => p.id === value)));
    requestAnimationFrame(() => searchRef.current?.focus());
  }
  function choose(id: string) {
    onChange(id);
    setOpen(false);
    triggerRef.current?.focus();
  }
  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === "Escape") {
      setOpen(false);
      triggerRef.current?.focus();
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      setHighlight((h) => Math.min(filtered.length - 1, h + 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setHighlight((h) => Math.max(0, h - 1));
    } else if (e.key === "Enter") {
      e.preventDefault();
      const p = filtered[highlight];
      if (p) choose(p.id);
    }
  }

  return (
    <div className="combo" ref={rootRef}>
      <button
        type="button"
        ref={triggerRef}
        className="combo__trigger"
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => (open ? setOpen(false) : openMenu())}
      >
        <span className="combo__trigger-text">
          <span className="combo__trigger-main">{selected?.name ?? "Select a product"}</span>
          {selected && (
            <span className="combo__trigger-sub">
              {inr(selected.price)} · {selected.stock > 0 ? `${selected.stock} in stock` : "OUT OF STOCK"}
            </span>
          )}
        </span>
        <span className={cx("combo__chevron", open && "combo__chevron--open")}>
          <Icon name="arrow" size={14} />
        </span>
      </button>

      {open && (
        <div className="combo__panel">
          <div className="combo__search">
            <Icon name="search" size={14} />
            <input
              ref={searchRef}
              value={query}
              onChange={(e) => {
                setQuery(e.target.value);
                setHighlight(0);
              }}
              onKeyDown={onKeyDown}
              placeholder="Search products…"
            />
          </div>
          <div className="combo__list" role="listbox">
            {filtered.length === 0 && (
              <div className="combo__empty">No products match “{query}”.</div>
            )}
            {filtered.map((p, i) => (
              <button
                type="button"
                key={p.id}
                role="option"
                aria-selected={p.id === value}
                className={cx("combo__item", i === highlight && "combo__item--active")}
                onMouseEnter={() => setHighlight(i)}
                onClick={() => choose(p.id)}
              >
                <span className="combo__item-main">
                  {p.name}
                  {p.id === value && <Icon name="check" size={13} />}
                </span>
                <span className="combo__item-sub">
                  {inr(p.price)} ·{" "}
                  {p.stock > 0 ? `${p.stock} in stock` : <span className="combo__oos">OUT OF STOCK</span>}
                </span>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

/* ---- Agent selector ------------------------------------------------- */
export function AgentSelect({
  agents,
  value,
  onChange,
}: {
  agents: Agent[];
  value: string;
  onChange: (id: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [highlight, setHighlight] = useState(0);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);

  const selected = agents.find((a) => a.id === value) ?? null;
  useOutsideClick(rootRef, () => setOpen(false));

  function openMenu() {
    setOpen(true);
    setHighlight(Math.max(0, agents.findIndex((a) => a.id === value)));
  }
  function choose(id: string) {
    onChange(id);
    setOpen(false);
    triggerRef.current?.focus();
  }
  function onKeyDown(e: React.KeyboardEvent) {
    if (!open) return;
    if (e.key === "Escape") {
      setOpen(false);
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      setHighlight((h) => Math.min(agents.length - 1, h + 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setHighlight((h) => Math.max(0, h - 1));
    } else if (e.key === "Enter") {
      e.preventDefault();
      const a = agents[highlight];
      if (a) choose(a.id);
    }
  }

  return (
    <div className="combo" ref={rootRef} onKeyDown={onKeyDown}>
      <button
        type="button"
        ref={triggerRef}
        className="combo__trigger"
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => (open ? setOpen(false) : openMenu())}
      >
        <span className="combo__trigger-text">
          <span className="combo__trigger-main">{selected?.name ?? "Select an agent"}</span>
          {selected && (
            <span className="combo__trigger-sub">
              {selected.type} · {selected.status}
            </span>
          )}
        </span>
        <span className={cx("combo__chevron", open && "combo__chevron--open")}>
          <Icon name="arrow" size={14} />
        </span>
      </button>

      {open && (
        <div className="combo__panel">
          <div className="combo__list" role="listbox">
            {agents.map((a, i) => (
              <button
                type="button"
                key={a.id}
                role="option"
                aria-selected={a.id === value}
                className={cx("combo__item", "combo__item--rich", i === highlight && "combo__item--active")}
                onMouseEnter={() => setHighlight(i)}
                onClick={() => choose(a.id)}
              >
                <div className="combo__item-top">
                  <span className="combo__item-main">{a.name}</span>
                  {a.id === value && <Icon name="check" size={13} />}
                </div>
                <div className="row" style={{ gap: 6, marginTop: 5 }}>
                  <span className="chip">{a.type}</span>
                  <span
                    className={cx("badge", a.status === "ACTIVE" ? "v-allow" : "v-deny")}
                    style={{ fontSize: 10 }}
                  >
                    {a.status}
                  </span>
                  <span className="chip chip--mono">cap {inr(a.max_transaction_amount)}</span>
                </div>
                <div className="muted" style={{ fontSize: 11, marginTop: 5 }}>
                  {a.allowed_actions.length > 0 ? a.allowed_actions.join(" · ") : "No actions permitted"}
                </div>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
