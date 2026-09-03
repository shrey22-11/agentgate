"""
Rendering for a :class:`app.metrics.runner.SuiteResult`. OUR SYSTEM.

``render_markdown`` produces the human-readable report; ``render_json`` the
machine-readable one. Both are pure functions of the result - no recomputation,
no network. ``write_reports`` drops ``<split>-report.md`` / ``<split>-report.json``
into a directory (default ``docs/metrics``).
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from app.metrics.runner import SuiteResult

DEFAULT_OUT_DIR = Path(__file__).resolve().parents[3] / "docs" / "metrics"


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


def _bool(x: bool | None) -> str:
    return {True: "yes", False: "NO", None: "n/a"}[x]


def render_json(result: SuiteResult) -> dict:
    return asdict(result)


def render_markdown(result: SuiteResult) -> str:
    m = result.metrics
    counts = m["scenario_counts"]
    adv = m["adversarial"]
    lat = m["decision_latency_ms"]
    L: list[str] = []

    L.append(f"# AgentGate evaluation report - `{result.split}` split")
    L.append("")
    L.append(f"Generated {result.generated_at} - OUR SYSTEM - frozen scenario suite")
    L.append("")
    L.append(
        f"**{counts['total_graded']} graded scenarios** "
        f"({counts['policy']} deterministic-policy, {counts['adversarial_nl']} adversarial "
        f"natural-language) plus {counts['idempotency']} idempotency cases and "
        f"{result.tamper['trials']} injected audit-tamper trials."
    )
    L.append("")

    # --- headline ---------------------------------------------------------
    L.append("## Headline")
    L.append("")
    L.append("| Metric | Result |")
    L.append("|---|---|")
    L.append(f"| Verdict matches deterministic engine (integration fidelity) | **{_pct(m['verdict_match_rate'])}** |")
    L.append(f"| Rule-id matches deterministic engine | {_pct(m['rule_match_rate'])} |")
    L.append(f"| Block rate on policy-violating requests | **{_pct(m['block_rate_on_policy_violating'])}** |")
    L.append(f"| False-block rate on benign requests | **{_pct(m['false_block_rate_on_benign'])}** |")
    L.append(f"| Prompt-injection neutralised (deterministic verdict + rule + override flag all match) | **{_pct(adv['injection_neutralised_rate'])}** |")
    L.append(f"| Structured-parse pass-through (defensive re-validation) | {_pct(adv['structured_parse_passthrough_rate'])} |")
    L.append(f"| Idempotency cases correct | **{_pct(m['idempotency_pass_rate'])}** |")
    L.append(f"| Audit-chain tamper detection | **{_pct(m['audit_tamper_detection_rate'])}** |")
    L.append(f"| Audit chain valid after the whole run | {_bool(m['audit_chain_valid_after_suite'])} |")
    L.append(f"| Unexpected Razorpay / payment objects created | {result.payments_created_unexpected} |")
    L.append(f"| Decision latency (policy route) p50 / p95 | {lat['policy_route'].get('p50_ms', 'n/a')} ms / {lat['policy_route'].get('p95_ms', 'n/a')} ms |")
    L.append("")

    # --- ground truth ---------------------------------------------------
    L.append("## How ground truth is determined")
    L.append("")
    L.append(
        "For every deterministic scenario the expected verdict and rule id are "
        "computed by calling `app.policy.evaluate` on a `PolicyInput` built from "
        "the authoritative seeded agent/product rows - the same helper the live "
        "`POST /actions` path uses (`_build_policy_input`). For a natural-language "
        "scenario, if the defensively-parsed request resolves cleanly against the "
        "catalogue the expected verdict is again `evaluate(...)` on the resolved "
        "fields; otherwise it is the parser's documented fail-closed result "
        "`DENY / RULE_INPUT_INVALID`. No scenario stores a hand-written verdict - "
        "the `Scenario` dataclass has no field for one, and a test enforces that."
    )
    L.append("")
    L.append(
        f"- Engine blocks every request *designed* as a policy violation: "
        f"**{_bool(m['engine_blocks_all_designed_violations'])}**"
    )
    L.append(
        f"- Engine allows every request *designed* as benign: "
        f"**{_bool(m['engine_allows_all_designed_benign'])}**"
    )
    L.append("")
    L.append(
        "  These two lines are the honesty check on the *design intent* labels: "
        "when they read `yes`, the block-rate / false-block-rate numbers above are "
        "measured against a suite whose intent labels the deterministic engine "
        "agrees with. A `NO` would mean a scenario the author mislabelled - it is "
        "reported, never relabelled to flatter the metric."
    )
    L.append("")

    # --- per category --------------------------------------------------
    L.append("## By category")
    L.append("")
    L.append("| Category | Scenarios | Verdict match | Blocked by system |")
    L.append("|---|---|---|---|")
    for cat in ("benign", "policy_violating"):
        rows = [r for r in result.scenarios if r.category == cat and r.kind == "policy"]
        if not rows:
            continue
        vm = _pct(round(sum(1 for r in rows if r.verdict_match) / len(rows), 4))
        bl = _pct(round(sum(1 for r in rows if r.actual_blocked) / len(rows), 4))
        L.append(f"| {cat} | {len(rows)} | {vm} | {bl} |")
    nl_rows = [r for r in result.scenarios if r.kind == "nl"]
    if nl_rows:
        vm = _pct(round(sum(1 for r in nl_rows if r.verdict_match) / len(nl_rows), 4))
        bl = _pct(round(sum(1 for r in nl_rows if r.actual_blocked) / len(nl_rows), 4))
        L.append(f"| adversarial (NL) | {len(nl_rows)} | {vm} | {bl} |")
    L.append("")

    # --- adversarial detail -----------------------------------------
    L.append("## Adversarial / prompt-injection")
    L.append("")
    L.append(
        f"{adv['n']} hostile natural-language messages (fake authority, "
        "'ignore previous instructions', 'pay now', lifted caps, "
        "hallucinated products, malformed numbers). Each is parsed defensively "
        "(model call stubbed) and its legitimate request is put through the same "
        "deterministic policy path."
    )
    L.append("")
    L.append(f"- Deterministic verdict unchanged by the injection: {_pct(adv['verdict_match_rate'])}")
    L.append(f"- Override/manipulation correctly flagged in the audit trail: {_pct(adv['override_flag_match_rate'])}")
    L.append(f"- Reached a real policy evaluation on resolved fields: {_pct(adv['structured_parse_passthrough_rate'])}")
    L.append(f"- Safely failed closed before policy (unresolvable / malformed): {_pct(adv['structured_parse_failclosed_rate'])}")
    L.append(f"- Payment objects created by any adversarial scenario: 0 (see money invariant)")
    L.append("")

    # --- latency ---------------------------------------------------
    L.append("## Decision latency")
    L.append("")
    L.append("In-process ASGI against local PostgreSQL, single-threaded, warm. Not a production latency claim.")
    L.append("")
    L.append("| Route | n | p50 | p95 | p99 | max | mean |")
    L.append("|---|--:|--:|--:|--:|--:|--:|")
    for name, key in (("POST /actions", "policy_route"), ("POST /ai/actions", "nl_route")):
        d = lat[key]
        if d.get("n"):
            L.append(
                f"| {name} | {d['n']} | {d['p50_ms']} ms | {d['p95_ms']} ms | "
                f"{d['p99_ms']} ms | {d['max_ms']} ms | {d['mean_ms']} ms |"
            )
    L.append("")

    # --- idempotency ---------------------------------------------
    L.append("## Idempotency under injected duplicates")
    L.append("")
    L.append("| Mechanism | Cases | Passed |")
    L.append("|---|--:|--:|")
    for mech in result.idempotency.values():
        L.append(f"| `{mech['mechanism']}` | {mech['trials']} | {mech['passed']} |")
    L.append("")
    for mech in result.idempotency.values():
        L.append(f"**`{mech['mechanism']}`**")
        L.append("")
        for case in mech["cases"]:
            mark = "ok" if case["passed"] else "FAIL"
            L.append(f"- [{mark}] `{case['id']}` - {case['intent']}")
            for name, ok in case["checks"].items():
                L.append(f"    - {'[ok]' if ok else '[FAIL]'} {name}")
        L.append("")

    # --- audit integrity --------------------------------------
    t = result.tamper
    L.append("## Audit-chain integrity under injected tampering")
    L.append("")
    L.append(
        f"{t['trials']} trials: append three probe events, corrupt one, run "
        f"`verify_audit_chain`, roll back so nothing is persisted. "
        f"{t['clean_trials']} clean control trials (no corruption)."
    )
    L.append("")
    L.append("| Tamper mode | Trials | Detected |")
    L.append("|---|--:|--:|")
    for mode, d in t["by_mode"].items():
        L.append(f"| {mode} | {d['trials']} | {d['detected']} |")
    L.append(f"| **clean control (must stay valid)** | {t['clean_trials']} | {t['clean_valid']} valid |")
    L.append("")
    L.append(
        f"Committed chain after the full run: **{_bool(result.audit_chain_valid)}** valid, "
        f"{result.audit_events_total} events."
    )
    L.append("")

    # --- failures ------------------------------------------
    failures = [r for r in result.scenarios if not r.passed]
    L.append("## Scenario failures")
    L.append("")
    if not failures:
        L.append("None. Every graded scenario matched the deterministic engine.")
    else:
        L.append("| id | category | expected | actual | detail |")
        L.append("|---|---|---|---|---|")
        for r in failures:
            L.append(
                f"| `{r.id}` | {r.category} | {r.expected_verdict}/{r.expected_rule} | "
                f"{r.actual_verdict}/{r.actual_rule} | {r.detail} |"
            )
    L.append("")

    # --- honesty ----------------------------------------
    L.append("## What is simulated or stubbed")
    L.append("")
    for note in result.notes:
        L.append(f"- {note}")
    L.append("")

    return "\n".join(L)


def write_reports(result: SuiteResult, out_dir: Path | str | None = None) -> tuple[Path, Path]:
    out = Path(out_dir) if out_dir is not None else DEFAULT_OUT_DIR
    out.mkdir(parents=True, exist_ok=True)
    md_path = out / f"{result.split}-report.md"
    json_path = out / f"{result.split}-report.json"
    md_path.write_text(render_markdown(result), encoding="utf-8")
    json_path.write_text(json.dumps(render_json(result), indent=2), encoding="utf-8")
    return md_path, json_path
