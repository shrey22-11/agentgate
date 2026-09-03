"""
Phase 12 - the evaluation harness itself.

Two kinds of test here:

  * cheap structural checks on the frozen scenario suite (count, uniqueness, the
    dev/holdout split, and the guarantee that a Scenario cannot carry a
    hand-written verdict);
  * two integration runs of ``run_suite`` against a disposable
    ``agentgate_metrics`` database (created on demand, exactly like the test DB):
    the full ``holdout`` split, and a small cross-section that exercises every
    idempotency mechanism and every tamper mode.

The harness must never weaken the honesty properties: ground truth comes from
``app.policy.evaluate``, no scenario is relabelled to flatter a metric, and no
Razorpay object is created anywhere.
"""
from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path

import pytest

from app.metrics import render_json, render_markdown, run_suite
from app.metrics.scenarios import DEV, HOLDOUT, SCENARIOS, Scenario, select

_FORBIDDEN_SCENARIO_FIELDS = {
    "expected", "expected_verdict", "verdict", "expected_rule", "rule_id",
    "ground_truth", "outcome", "expect", "label", "gold",
}
_METRICS_DIR = Path(__file__).resolve().parents[1] / "app" / "metrics"


# ===========================================================================
# structural checks (no DB)
# ===========================================================================
def test_suite_is_frozen_and_large() -> None:
    assert len(SCENARIOS) >= 100
    ids = [s.id for s in SCENARIOS]
    assert len(ids) == len(set(ids)), "scenario ids must be unique"

    # frozen: exact composition. If these change the split must be re-reviewed.
    assert len(SCENARIOS) == 123
    assert (len(DEV), len(HOLDOUT)) == (82, 41)
    # every scenario is in exactly one split; the splits preserve suite order.
    assert {s.split for s in SCENARIOS} == {"dev", "holdout"}
    assert len(DEV) + len(HOLDOUT) == len(SCENARIOS)
    assert list(DEV) == [s for s in SCENARIOS if s.split == "dev"]
    assert list(HOLDOUT) == [s for s in SCENARIOS if s.split == "holdout"]
    # the split is positional and stable: every third scenario is holdout.
    assert [s.split for s in SCENARIOS] == [
        "holdout" if i % 3 == 2 else "dev" for i in range(len(SCENARIOS))
    ]

    # deterministic: re-selecting yields the same objects, same order.
    assert select("all") is SCENARIOS
    assert [s.id for s in select("dev")] == [s.id for s in DEV]
    assert [s.id for s in select("holdout")] == [s.id for s in HOLDOUT]


def test_every_split_covers_every_category() -> None:
    cats = {"benign", "policy_violating", "adversarial", "idempotency"}
    assert {s.category for s in DEV} == cats
    assert {s.category for s in HOLDOUT} == cats


def test_scenario_cannot_carry_a_hand_written_verdict() -> None:
    names = {f.name for f in dataclasses.fields(Scenario)}
    assert not (names & _FORBIDDEN_SCENARIO_FIELDS), (
        "Scenario must not have an expected/ground-truth field"
    )
    # the frozen data file never imports or references the Verdict enum, and
    # `RULE_*` names appear only in explanatory comments, never in scenario data.
    src = (_METRICS_DIR / "scenarios.py").read_text(encoding="utf-8")
    assert "Verdict" not in src
    code_lines = [ln for ln in src.splitlines() if "RULE_" in ln]
    assert code_lines and all(ln.lstrip().startswith("#") for ln in code_lines), (
        "RULE_* may only appear in comments in scenarios.py"
    )


def test_ground_truth_is_computed_from_the_real_engine() -> None:
    src = (_METRICS_DIR / "ground_truth.py").read_text(encoding="utf-8")
    assert "from app.policy import evaluate" in src
    assert "_build_policy_input" in src
    assert "evaluate(_build_policy_input(" in src
    # the only rule literal allowed is the documented fail-closed result
    rule_literals = set(re.findall(r"RULE_[A-Z_]+", src))
    assert rule_literals <= {"RULE_INPUT_INVALID"}


def test_metrics_package_imports_no_provider_sdk() -> None:
    bad = re.compile(r"^\s*(?:import|from)\s+(anthropic|openai|razorpay)\b", re.M)
    for path in sorted(_METRICS_DIR.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert not bad.search(text), f"{path.name} imports a provider SDK directly"
        assert " float(" not in text and "=float(" not in text, f"{path.name}: no float() on money"


# ===========================================================================
# integration: full holdout split
# ===========================================================================
@pytest.mark.asyncio
async def test_holdout_split_matches_the_deterministic_engine() -> None:
    result = await run_suite("holdout")
    m = result.metrics

    # integration fidelity: the DB-backed path agrees with the pure engine.
    assert m["verdict_match_rate"] == 1.0, [
        (r.id, r.expected_verdict, r.actual_verdict) for r in result.scenarios if not r.verdict_match
    ]
    assert m["rule_match_rate"] == 1.0
    assert m["suite_pass_rate"] == 1.0

    # design-intent labels line up with the engine (nothing relabelled).
    assert m["engine_blocks_all_designed_violations"] is True
    assert m["engine_allows_all_designed_benign"] is True
    assert m["block_rate_on_policy_violating"] == 1.0
    assert m["false_block_rate_on_benign"] == 0.0

    # adversarial: injection changes nothing, nothing charged.
    assert m["adversarial"]["injection_neutralised_rate"] == 1.0
    assert m["adversarial"]["override_flag_match_rate"] == 1.0
    assert result.payments_created_unexpected == 0

    # audit + idempotency.
    assert result.audit_chain_valid is True
    assert m["audit_tamper_detection_rate"] == 1.0
    assert m["audit_clean_control_valid_rate"] == 1.0
    assert m["idempotency_pass_rate"] == 1.0

    # latency summary is populated.
    pol = result.metrics["decision_latency_ms"]["policy_route"]
    assert pol["n"] > 0 and pol["p50_ms"] >= 0 and pol["p95_ms"] >= pol["p50_ms"]

    # reports render and round-trip.
    md = render_markdown(result)
    assert "# AgentGate evaluation report" in md and "Scenario failures" in md
    assert "None. Every graded scenario matched the deterministic engine." in md
    json.dumps(render_json(result))  # must be serialisable


# ===========================================================================
# integration: a small cross-section (every mechanism + every tamper mode)
# ===========================================================================
_CROSS_SECTION = {
    "benign-trailblaze-list-q1",
    "benign-velocity-disc10-at-cap",
    "gate-cap-treadmill-list",
    "counter-velocity-disc20",
    "violate-inactive-trailblaze",
    "violate-noperm-trailblaze",
    "violate-stock-cloudstep-q1",
    "violate-input-velocity-contradiction",
    "adv-inject-velocity-60off-paynow",
    "adv-hallucinated-product",
    "adv-not-a-purchase",
    "adv-clean-trailblaze-control",
    "idem-dup-action-counter",
    "idem-dup-action-needs-approval",
    "idem-payment-key-trailblaze",
    "idem-webhook-paid",
    "idem-webhook-unknown",
}


@pytest.mark.asyncio
async def test_cross_section_run() -> None:
    result = await run_suite("all", subset=_CROSS_SECTION)
    by_id = {r.id: r for r in result.scenarios}

    assert {r.id for r in result.scenarios} == {
        s for s in _CROSS_SECTION if not s.startswith("idem-")
    }
    assert all(r.passed for r in result.scenarios), [
        (r.id, r.detail) for r in result.scenarios if not r.passed
    ]

    # verdict spread is real, not all one value.
    verdicts = {r.actual_verdict for r in result.scenarios}
    assert {"ALLOW", "DENY", "NEEDS_APPROVAL", "COUNTER_OFFER"} <= verdicts

    # the hero injection lands on the deterministic counter-offer, override flagged.
    hero = by_id["adv-inject-velocity-60off-paynow"]
    assert hero.actual_verdict == "COUNTER_OFFER" and hero.reached_policy is True
    assert hero.override_flagged is True

    # unresolvable / non-purchase injections fail closed before policy.
    assert by_id["adv-hallucinated-product"].reached_policy is False
    assert by_id["adv-not-a-purchase"].reached_policy is False
    assert by_id["adv-hallucinated-product"].actual_rule == "RULE_INPUT_INVALID"

    # the polite control is allowed and not flagged as manipulation.
    ctrl = by_id["adv-clean-trailblaze-control"]
    assert ctrl.actual_verdict == "ALLOW" and ctrl.override_flagged is False

    # every idempotency mechanism ran and passed.
    mechs = result.idempotency
    assert set(mechs) == {"duplicate_action", "payment_attempt_key", "webhook_event"}
    for mech in mechs.values():
        assert mech["passed"] == mech["trials"] >= 1

    # every tamper mode was exercised and caught; clean controls stayed valid.
    for mode, d in result.tamper["by_mode"].items():
        assert d["trials"] >= 1 and d["detected"] == d["trials"], mode
    assert result.tamper["clean_valid"] == result.tamper["clean_trials"]

    assert result.payments_created_unexpected == 0
    assert result.audit_chain_valid is True
