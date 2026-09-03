"""
CLI for the evaluation harness.

    python -m app.metrics --split holdout
    python -m app.metrics --split dev
    python -m app.metrics --split all --out docs/metrics

Writes ``<split>-report.md`` and ``<split>-report.json`` to ``--out`` (default
``docs/metrics``) and prints the markdown to stdout. Exit code is non-zero only
on a hard failure: the audit chain went invalid, the full path disagreed with
the deterministic engine, an idempotency guarantee broke, a tamper went
undetected, or an unexpected payment object was created.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

for _stream in (sys.stdout, sys.stderr):
    try:  # Windows consoles default to cp1252; the report is UTF-8.
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

from app.metrics.report import DEFAULT_OUT_DIR, render_markdown, write_reports
from app.metrics.runner import SuiteResult, run_suite


def _hard_failure(result: SuiteResult) -> list[str]:
    m = result.metrics
    problems: list[str] = []
    if not result.audit_chain_valid:
        problems.append("audit chain is invalid after the run")
    if (m["verdict_match_rate"] or 0) < 1.0:
        problems.append(f"verdict-match rate {m['verdict_match_rate']} < 1.0 (integration drift)")
    if (m["rule_match_rate"] or 0) < 1.0:
        problems.append(f"rule-match rate {m['rule_match_rate']} < 1.0")
    if result.payments_created_unexpected != 0:
        problems.append(f"{result.payments_created_unexpected} unexpected payment object(s) created")
    if (m["audit_tamper_detection_rate"] or 0) < 1.0:
        problems.append(f"tamper detection rate {m['audit_tamper_detection_rate']} < 1.0")
    if (m["idempotency_pass_rate"] or 0) < 1.0:
        problems.append(f"idempotency pass rate {m['idempotency_pass_rate']} < 1.0")
    if m["engine_blocks_all_designed_violations"] is False:
        problems.append("a scenario labelled policy_violating is ALLOWed by the engine")
    if m["engine_allows_all_designed_benign"] is False:
        problems.append("a scenario labelled benign is blocked by the engine")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m app.metrics",
        description="AgentGate scenario harness + honest metrics (Phase 12)",
    )
    ap.add_argument("--split", choices=["dev", "holdout", "all"], default="all")
    ap.add_argument("--out", default=None, help=f"report directory (default: {DEFAULT_OUT_DIR})")
    ap.add_argument("--database-url", default=None, help="override the metrics database URL")
    ap.add_argument("--quiet", action="store_true", help="do not print the report to stdout")
    args = ap.parse_args(argv)

    result = asyncio.run(run_suite(args.split, database_url=args.database_url))

    if not args.quiet:
        print(render_markdown(result))

    md_path, json_path = write_reports(result, args.out)
    print(f"\nwrote {md_path}\nwrote {json_path}", file=sys.stderr)

    problems = _hard_failure(result)
    if problems:
        print("\nHARD FAILURES:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
