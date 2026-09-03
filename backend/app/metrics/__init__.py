"""
Evaluation harness (Phase 12). OUR SYSTEM.

A frozen suite of 120+ synthetic commercial requests — benign, policy-violating,
adversarial natural language, and duplicate/idempotency cases — run against the
real decision path, scored against ground truth computed from the deterministic
policy engine (never hand-labelled), and summarised as an honest report:
verdict-match / integration fidelity, block rate on violations, false-block rate
on benign requests, structured-parse pass-through, decision latency (p50/p95),
idempotency correctness under injected duplicates, and audit-chain integrity
under injected tampering.

    python -m app.metrics --split holdout      # writes docs/metrics/holdout-report.{md,json}

No revenue / conversion / AOV / business-impact figure is produced or implied.
"""
from __future__ import annotations

from app.metrics.report import render_json, render_markdown, write_reports
from app.metrics.runner import ScenarioResult, SuiteResult, run_suite
from app.metrics.scenarios import DEV, HOLDOUT, SCENARIOS, Scenario

__all__ = [
    "run_suite",
    "SuiteResult",
    "ScenarioResult",
    "render_markdown",
    "render_json",
    "write_reports",
    "SCENARIOS",
    "DEV",
    "HOLDOUT",
    "Scenario",
]
