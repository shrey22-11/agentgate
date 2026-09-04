# Frontend (Phase 11; payment UX + light redesign in Phase 14)

React 18 + TypeScript + Vite. Light, minimal, fintech-dashboard theme (see
*Design system* below — Phase 14 replaced the original dark/glass look).
**No runtime dependencies beyond `react` / `react-dom`** — routing is a
`useState` switch, motion is CSS `@keyframes`, icons are inline SVG,
data-fetching is a ~30-line `useAsync` hook. Served same-origin by the FastAPI
process in production (no CORS); proxied to `:8000` in dev.

The one exception to the `useState` routing is the Razorpay payment-return
visit: `App.tsx` checks `?payment_callback=1&decision_id=...` on the *initial*
render (see `readPaymentCallback()`) and, if present, renders the standalone
`PaymentResult` screen instead of the normal shell — no router, no new backend
route needed, since it is still just `/` with a query string.

## Screens

| # | Screen | Purpose | Backend calls |
|---|---|---|---|
| 1 | **AI Buyer Console** | The control boundary, front and centre. Three modes share one decision panel, which now ends in a verdict-specific payment action (Pay Now / accept a counter-offer / a link to the Approval Queue). | `POST /actions` · `POST /ai/actions` · `POST /ai/buyer` · `GET /catalog/agents` · `GET /catalog/products` · `POST /payments/{id}/execute` |
| — | **Payment Result** *(not in the sidebar — a Razorpay-return landing page, see below)* | Polished PAID / processing / FAILED / EXPIRED states for the customer coming back from Razorpay. Polls briefly, never trusts the callback's own query params. | `GET /payments/{decision_id}` |
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

All three land on the same **DecisionReveal** card — a colour-coded edge keyed
to `ALLOW` / `DENY` / `NEEDS_APPROVAL` / `COUNTER_OFFER`, the rule id, the
reason, the trusted `executable_amount`, and (for a counter-offer) the engine's
price with the note *"computed by the deterministic engine — not the LLM"*.
Structured mode additionally shows a **PurchaseSummary** receipt above it
(product / qty / list price / discount / final price / total) built from
client-side form state plus `executable_amount` — never a client-computed
total.

Below the decision, **PaymentAction** renders the one verdict-appropriate next
step — never more than one is shown:

- `ALLOW` → **Pay Now ₹X**, disabled while the request is in flight (guards
  against a double-click creating two payment links; the backend is also
  idempotent per decision either way). On success, navigates the browser to the
  Razorpay `short_url`; on failure, shows the error inline without discarding
  the decision.
- `NEEDS_APPROVAL` → the requested amount vs. the agent's limit, and a link to
  the Approval Queue — never Pay Now (a fresh `NEEDS_APPROVAL` decision has no
  `Approval` row yet; execution would 409).
- `COUNTER_OFFER` → the deterministic floor price and **Accept counter-offer**,
  which resubmits the same product/quantity as `ACCEPT_COUNTER_OFFER` (the
  policy engine re-evaluates it from scratch — the accept button never just
  marks the old decision paid). Only available where the product id + quantity
  are known client-side (Structured mode always; Natural Language once
  `resolved_product_id` comes back) — Buyer Agent mode shows a note instead,
  since the agent negotiates within its own run.
- `DENY` → nothing here; the DecisionReveal card's rule + reason is the whole
  story.

When `AI_ENABLED=false`, the NL and agent modes surface a clear amber banner
pointing the user to Structured mode; the app stays fully usable.

### Payment Result screen

Reached only via a Razorpay redirect (`?payment_callback=1&decision_id=...`).
Polls `GET /payments/{decision_id}` (local DB read, no Razorpay call) every 3s
for up to ~60s while the status is `CREATED`/`PENDING`, then falls back to a
manual "Check again" button rather than guessing. Renders `PAID` / still
processing / `FAILED` / `EXPIRED`; never reads Razorpay's own `razorpay_*`
query params for anything status-related (see
`docs/payment-execution.md` → *Customer payment-return flow*). Internal UUIDs
are not shown — only the amount and, once paid, the Razorpay payment-link id as
a reference.

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

Light, minimal, fintech-dashboard aesthetic (Razorpay-*inspired*, not a copy —
own brand mark, own colours, no borrowed assets): white/near-white surfaces,
hairline borders, soft low-elevation shadows instead of glow or
`backdrop-filter`, a solid-colour primary button instead of a gradient. Premium
comes from spacing/typography/hierarchy, deliberately not from motion — the few
animations left are subtle and functional: `settleIn`/`fadeUp` (a small
translate+fade on new cards), `shimmer` (skeletons), a toast slide-in. `.spinner`
uses `currentColor` so it reads correctly inside any button variant (white on
the brand-coloured Pay Now button, dark ink inside a ghost button). Same CSS
custom-property token architecture as before (`--bg-*`, `--text-*`, `--brand`,
per-verdict colours), same utility classes (`.card`, `.btn`, `.badge`, `.grid
cols-N`, …) — Phase 14 re-themed the tokens and trimmed the motion, it did not
change the class architecture, so no component needed a rewrite for the
redesign itself. New component classes added for the payment flow: `.receipt*`
(purchase summary), `.negotiate*` (counter-offer), `.approval-block*`,
`.pay-bar`, `.result-*`/`.status-icon*` (the payment-result page). Fully
responsive — the sidebar collapses to an icon rail under 900px, grids reflow,
wide tables scroll inside their own container. `@media
(prefers-reduced-motion: reduce)` disables all animation.

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
frontend/src/components/PurchaseSummary.tsx    Structured-mode order receipt
frontend/src/components/PaymentAction.tsx      Pay Now / approval-status / accept-counter-offer
frontend/src/screens/{BuyerConsole,AuditTimeline,Dashboard,Approvals,Catalog}.tsx
frontend/src/screens/PaymentResult.tsx         Razorpay-return landing page
```

## Verification

- `npm run build` (`tsc -b` strict typecheck + `vite build`) — passes clean.
- Backend serves `frontend/dist` at `/` (verified live); all five sidebar
  screens' read endpoints return correct data against a seeded DB, plus the new
  `GET /payments/{decision_id}` and `POST /payments/{decision_id}/execute`
  exercised directly against the seeded catalogue (ALLOW / COUNTER_OFFER /
  NEEDS_APPROVAL / ACCEPT_COUNTER_OFFER, and the `503`/`404`/`409` error paths
  with `RAZORPAY_ENABLED=false`); audit chain reports `valid: true`.
- No real Gemini / Razorpay call is made by the UI beyond what the user
  triggers; the NL and buyer-agent modes require `AI_ENABLED=true` + a key to do
  anything (otherwise a graceful `503` banner). The Pay Now button needs
  `RAZORPAY_ENABLED=true` + real test-mode keys for the same reason — with it
  `false`, clicking Pay Now surfaces the `RAZORPAY_DISABLED` error inline.
- The full Razorpay-return round trip (`callback_url` → Payment Result page →
  webhook flips the status → page reflects it) is implemented and covered by
  the payment-execution test suite against a fake Razorpay client, but has not
  been exercised against a live test-mode payment in this environment (no
  Razorpay test-mode credentials available here) — see
  `docs/payment-execution.md`.

## Limitations

- No client-side tests (`vitest` is wired but unused this phase — the strict
  build + the backend suite are the gate).
- No SSE/websocket; screens poll on mount and via explicit "Refresh" buttons
  (the sidebar's health + pending-approval count poll every 15 s; the Payment
  Result screen polls its one endpoint for up to ~60s after a payment).
- The buyer transcript is shown live but not persisted (no schema for it).
- Accepting a counter-offer from the UI needs the product id + quantity as
  client-side state, which Buyer Agent mode does not expose (the agent decides
  autonomously within its own run instead).
