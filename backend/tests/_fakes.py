"""
Test double for the Razorpay boundary. NOT a Razorpay simulator — it stands in
for `app.razorpay.client.RazorpayClient` so payment-execution and webhook logic
can be tested without network access. Real integration is verified separately
(see docs/payment-execution.md).
"""
from __future__ import annotations

import hmac
from dataclasses import replace

import itertools

from app.ai.client import AIError, BuyerStep, BuyerToolCall
from app.ai.schemas import ParsedIntent
from app.razorpay.client import PaymentLinkResult, RazorpayError, hmac_sha256_hex

FAKE_WEBHOOK_SECRET = "whsec_fake_for_tests"


class FakeAIParserClient:
    """
    Stands in for `app.ai.client.AIParserClient`. Give it either a `result`
    (a ParsedIntent to return) or an `error` (an exception to raise). No
    network, no anthropic SDK.
    """

    def __init__(
        self,
        *,
        result: ParsedIntent | None = None,
        error: AIError | Exception | None = None,
    ) -> None:
        self._result = result
        self._error = error
        self.calls: list[str] = []

    async def parse_intent(self, *, raw_input: str) -> ParsedIntent:
        self.calls.append(raw_input)
        if self._error is not None:
            raise self._error
        assert self._result is not None, "FakeAIParserClient needs result or error"
        return self._result


_tool_id = itertools.count(1)


def tool_step(*calls: tuple[str, dict], text: str = "") -> BuyerStep:
    """Build a `tool_calls` BuyerStep from (tool_name, input_dict) pairs."""
    tcs = [
        BuyerToolCall(id=f"toolu_{next(_tool_id):04d}", name=name, input=dict(inp))
        for name, inp in calls
    ]
    return BuyerStep(
        kind="tool_calls",
        text=text,
        tool_calls=tcs,
        assistant_content=[
            {"type": "tool_use", "id": c.id, "name": c.name, "input": c.input}
            for c in tcs
        ],
    )


def final_step(text: str) -> BuyerStep:
    return BuyerStep(kind="final", text=text, assistant_content=[{"type": "text", "text": text}])


class FakeAIBuyerClient:
    """
    Scripted stand-in for `app.ai.client.AIBuyerClient`. Returns the given
    BuyerSteps in order; raises `error` (optionally only after `error_after`
    successful steps). Ignores `messages` — the script drives the run.
    """

    def __init__(
        self,
        *,
        script: list[BuyerStep] | None = None,
        error: AIError | Exception | None = None,
        error_after: int = 0,
    ) -> None:
        self._script = list(script or [])
        self._error = error
        self._error_after = error_after
        self.step_calls = 0
        self.tool_defs_seen: list[dict] = []

    async def next_step(self, *, messages, tools) -> BuyerStep:
        self.step_calls += 1
        self.tool_defs_seen = tools
        if self._error is not None and self.step_calls > self._error_after:
            raise self._error
        if self._script:
            return self._script.pop(0)
        return final_step("(script exhausted)")


class FakeRazorpayClient:
    def __init__(self, *, webhook_secret: str = FAKE_WEBHOOK_SECRET, fail_create: bool = False):
        self.webhook_secret = webhook_secret
        self.fail_create = fail_create
        self.create_calls: list[dict] = []
        self._by_reference: dict[str, PaymentLinkResult] = {}
        self._seq = 0

    async def create_payment_link(
        self,
        *,
        amount_paise,
        currency,
        reference_id,
        description,
        notes,
        callback_url=None,
        callback_method=None,
    ) -> PaymentLinkResult:
        self.create_calls.append(
            {
                "amount_paise": amount_paise,
                "currency": currency,
                "reference_id": reference_id,
                "notes": notes,
                "callback_url": callback_url,
                "callback_method": callback_method,
            }
        )
        if self.fail_create:
            raise RazorpayError("payment_link.create failed: FakeInducedError")
        self._seq += 1
        pid = f"plink_test_{self._seq:04d}"
        result = PaymentLinkResult(
            id=pid,
            short_url=f"https://rzp.test/i/{pid}",
            status="created",
            amount_paise=amount_paise,
            raw={"reference_id": reference_id, "notes": notes},
        )
        self._by_reference[reference_id] = result
        return result

    async def fetch_payment_links_by_reference(self, reference_id: str) -> list[PaymentLinkResult]:
        found = self._by_reference.get(reference_id)
        return [found] if found is not None else []

    def verify_webhook_signature(self, *, raw_body: bytes, signature: str) -> bool:
        expected = hmac_sha256_hex(self.webhook_secret, raw_body)
        return hmac.compare_digest(expected, signature or "")

    # --- test-only helpers ---------------------------------------------
    def sign(self, raw_body: bytes) -> str:
        return hmac_sha256_hex(self.webhook_secret, raw_body)

    def set_reference_status(self, reference_id: str, status: str) -> None:
        self._by_reference[reference_id] = replace(
            self._by_reference[reference_id], status=status
        )

    def register_link(self, reference_id: str, link_id: str, status: str = "created") -> None:
        self._by_reference[reference_id] = PaymentLinkResult(
            id=link_id,
            short_url=f"https://rzp.test/i/{link_id}",
            status=status,
            amount_paise=0,
            raw={},
        )
