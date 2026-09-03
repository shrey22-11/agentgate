# Frontend (Phase 11)

React 18 + TypeScript + Vite. Dark, single committed theme. **No runtime
dependencies beyond `react` / `react-dom`** — routing is a `useState` switch,
motion is CSS `@keyframes`, icons are inline SVG, data-fetching is a ~30-line
`useAsync` hook. Served same-origin by the FastAPI process in production (no
CORS); proxied to `:8000` in dev.

## Screens

| # | Screen | Purpose | Backend calls |
|---|---|---|---|
| 1 | **AI Buyer Console** | The control boundary, front and centre. Three modes share one decision panel. | `POST /actions` · `POST /ai/actions` · `POST /ai/buyer` · `GET /catalog/agents` · `GET /catalog/products` |
| 2 | **Audit Timeline** | The hash chain, visualised: every event with `prev_hash → hash` linkage, a live "linked ✓ / BROKEN ✕" check, expandable canonical payloads, and a top-line integrity verdict. | `GET /audit/events?limit=` · `GET /audit/chain` |
| 3 | **Merchant Dashboard** | Counts of AgentGate's decisions over the SIMULATED population. Animated count-up tiles, a verdict-mix bar, a recent-decisions table. No revenue/conversion figures. | `GET /dashboard/summary` |
| 4 | **Approval Queue** | `NEEDS_APPROVAL` decisions awaiting a human. Approve / reject with a resolver identity + optional reason; the badge in the sidebar tracks the count. | `GET /approvals/pending` · `POST /approvals/{id}/approve` · `POST /approvals/{id}/reject` |
| 5 | **Products & Policy** | The commercial inputs the engine reads — list price, stock, max discount, margin floor — plus a computed **"floor at cap"** column (`max(price·(1−maxDisc), marginFloor)`), and the agent roster with limits. | `GET /catalog/products` · `GET /catalog/agents` |

### Screen 1 — three modes

- **Structured** — product picker + quantity/discount sliders → `POST /actions`.
  Works with **no AI key**; exercises the exact same deterministic policy path.
- **Natural language** — free text → `POST /ai/actions`. Shows `confidence`, the
  resolved product, and a "manipulation flagged" badge when
  `override_instructions_detected` is true (recorded, not obeyed).
- **Buyer agent** — a shopping goal → `POST /ai/buyer`. Renders the full
  transcript (model text / tool calls / tool results) and the run `outcome`.

All three land on the same **DecisionReveal** card — verdict colour + glow keyed
to `ALLOW` / `DENY` / `NEEDS_APPROVAL` / `COUNTER_OFFER`, the rule id, the reason,
and (for a counter-offer) the engine's price with the note *"computed by the
deterministic engine — not the LLM"*.

When `AI_ENABLED=false`, the NL and agent modes surface a clear amber banner
pointing the user to Structured mode; the app stays fully usable.

## Backend read layer added for this phase

Three new read-only routers (`GET` only, no auth, one round-trip each):

- `app/catalog/router.py` — `GET /catalog/products`, `GET /catalog/agents`
  (merchant-facing: **includes** `max_discount_pct` / `min_margin_price` and
  agent limits, unlike the buyer-agent's `app.catalog.queries` views).
- `app/audit/router.py` — `GET /audit/events` (newest-first, `limit` 1–1000,
  optional `ref_id`), `GET /audit/chain` (`verify_audit_chain()` as JSON).
- `app/dashboard/router.py` — `GET /dashboard/summary` (aggregate counts +
  12 recent decisions).

Covered by `backend/tests/test_read_api.py` (5 tests).

## Design system (`src/styles.css`)

CSS custom properties for the palette; glass cards
(`backdrop-filter: blur`, hairline borders); an ambient animated gradient-mesh
backdrop with a grain overlay; `@keyframes` for `stampIn` (decision reveal),
`shimmer` (skeletons), `pulseGlow`, `fadeUp` (staggered lists), toast slide-in;
gradient buttons with a hover sheen. Fully responsive — the sidebar collapses to
an icon rail under 900px, grids reflow, wide tables scroll inside their own
container. `@media (prefers-reduced-motion: reduce)` disables all animation.

Fonts: **Inter** + **JetBrains Mono** via Google Fonts `<link>` in `index.html`
(this app is not an Artifact — no CSP allowlist applies; it degrades to the
system stack if the CDN is unreachable).

## Running it

```bash
# dev (hot reload) — backend on :8000, SPA on :5173 with a proxy
docker compose up -d db
cd backend && ./.venv/Scripts/python -m uvicorn app.main:app --reload
cd frontend && npm install && npm run dev      # http://localhost:5173

# production-style — one origin, backend serves the built SPA
cd frontend && npm run build                   # -> frontend/dist
cd backend && ./.venv/Scripts/python -m uvicorn app.main:app   # http://localhost:8000
# or: docker compose up --build
```

## Files

```
frontend/index.html                       fonts, meta, mount
frontend/vite.config.ts                   dev proxy for every backend prefix
frontend/src/main.tsx                     entry (unchanged)
frontend/src/App.tsx                      shell: sidebar nav, view switch, health/AI probe
frontend/src/styles.css                   the design system
frontend/src/api.ts                       typed fetch client + response types
frontend/src/ui.tsx                       Button/Field/Skeleton/VerdictBadge/StatTile/Banner/Icon/useAsync/useToast/useCountUp
frontend/src/components/DecisionReveal.tsx
frontend/src/components/HashChain.tsx
frontend/src/components/BuyerTranscript.tsx
frontend/src/screens/{BuyerConsole,AuditTimeline,Dashboard,Approvals,Catalog}.tsx
```

## Verification

- `npm run build` (`tsc -b` strict typecheck + `vite build`) — passes clean.
- Backend serves `frontend/dist` at `/` (verified live); all five screens' read
  endpoints return correct data against a seeded DB; audit chain reports
  `valid: true`.
- No real Anthropic / Razorpay call is made by the UI beyond what the user
  triggers; the NL and buyer-agent modes require `AI_ENABLED=true` + a key to do
  anything (otherwise a graceful `503` banner).

## Limitations

- No client-side tests (`vitest` is wired but unused this phase — the strict
  build + the backend suite are the gate).
- No SSE/websocket; screens poll on mount and via explicit "Refresh" buttons
  (the sidebar's health + pending-approval count poll every 15 s).
- The buyer transcript is shown live but not persisted (no schema for it).
- Payment execution (`POST /payments/{id}/execute`) is **not** wired into the UI
  — it needs real Razorpay test-mode credentials; the dashboard shows payment
  status counts if any exist.
