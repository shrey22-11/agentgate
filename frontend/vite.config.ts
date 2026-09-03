import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In production the backend serves this build same-origin (Section H/L), so
// there is no CORS surface at all and relative fetch paths just work. In local
// dev the SPA and backend run as two dev servers; the proxy below forwards the
// backend's API prefixes to :8000 so that arrangement stays CORS-free too.
const backend = "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: Object.fromEntries(
      [
        "/health",
        "/actions",
        "/ai",
        "/approvals",
        "/payments",
        "/webhooks",
        "/audit",
        "/catalog",
        "/dashboard",
      ].map((p) => [p, backend]),
    ),
  },
});
