"""
The Razorpay boundary. The `razorpay` SDK is imported *only* here — no other
module knows an order id from a payment-link id at the SDK level.

`RazorpayClient` is a small async protocol. `SdkRazorpayClient` wraps the
synchronous SDK and runs each call in a worker thread so the event loop is not
blocked. `DisabledRazorpayClient` is what `get_razorpay_client()` returns when
`RAZORPAY_ENABLED` is false — every call raises `RazorpayDisabledError`, which
the routers turn into HTTP 503.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.core.config import get_settings


class RazorpayError(Exception):
    """Any failure talking to Razorpay (network, API error, unexpected shape)."""


class RazorpayDisabledError(Exception):
    """
    Razorpay execution is turned off for this deployment. Deliberately NOT a
    subclass of RazorpayError: "disabled" is a configuration state, not a
    payment failure, so the execution service must not treat it as one.
    """


@dataclass(frozen=True)
class PaymentLinkResult:
    id: str
    short_url: str
    status: str
    amount_paise: int
    raw: dict[str, Any] = field(default_factory=dict)


class RazorpayClient(Protocol):
    async def create_payment_link(
        self,
        *,
        amount_paise: int,
        currency: str,
        reference_id: str,
        description: str,
        notes: dict[str, str],
    ) -> PaymentLinkResult: ...

    async def fetch_payment_links_by_reference(
        self, reference_id: str
    ) -> list[PaymentLinkResult]: ...

    def verify_webhook_signature(self, *, raw_body: bytes, signature: str) -> bool: ...


def _to_result(entity: dict[str, Any]) -> PaymentLinkResult:
    return PaymentLinkResult(
        id=str(entity["id"]),
        short_url=str(entity.get("short_url", "")),
        status=str(entity.get("status", "")),
        amount_paise=int(entity.get("amount", 0)),
        raw=entity,
    )


class SdkRazorpayClient:
    """REAL RAZORPAY. Wraps `razorpay.Client` (test-mode credentials)."""

    def __init__(self, *, key_id: str, key_secret: str, webhook_secret: str) -> None:
        import razorpay  # imported here so a disabled deployment never needs it

        self._sdk = razorpay.Client(auth=(key_id, key_secret))
        self._sdk.set_app_details({"title": "AgentGate", "version": "phase8"})
        self._webhook_secret = webhook_secret
        self._errors = razorpay.errors

    async def create_payment_link(
        self,
        *,
        amount_paise: int,
        currency: str,
        reference_id: str,
        description: str,
        notes: dict[str, str],
    ) -> PaymentLinkResult:
        data = {
            "amount": amount_paise,
            "currency": currency,
            "accept_partial": False,
            "reference_id": reference_id,
            "description": description,
            "notes": notes,
            "reminder_enable": False,
        }
        try:
            entity = await asyncio.to_thread(self._sdk.payment_link.create, data)
        except Exception as exc:  # SDK raises requests/BadRequestError subclasses
            raise RazorpayError(f"payment_link.create failed: {type(exc).__name__}") from exc
        if not isinstance(entity, dict) or "id" not in entity:
            raise RazorpayError("payment_link.create returned an unexpected shape")
        return _to_result(entity)

    async def fetch_payment_links_by_reference(
        self, reference_id: str
    ) -> list[PaymentLinkResult]:
        try:
            page = await asyncio.to_thread(
                self._sdk.payment_link.all, {"reference_id": reference_id}
            )
        except Exception as exc:
            raise RazorpayError(
                f"payment_link.all failed: {type(exc).__name__}"
            ) from exc
        items = page.get("payment_links", []) if isinstance(page, dict) else []
        return [_to_result(item) for item in items if isinstance(item, dict) and item.get("id")]

    def verify_webhook_signature(self, *, raw_body: bytes, signature: str) -> bool:
        # Razorpay's documented mechanism: HMAC-SHA256 hex of the raw body with
        # the *webhook* secret, constant-time compared to X-Razorpay-Signature.
        try:
            self._sdk.utility.verify_webhook_signature(
                raw_body.decode("utf-8"), signature, self._webhook_secret
            )
            return True
        except self._errors.SignatureVerificationError:
            return False
        except Exception:
            return False


class DisabledRazorpayClient:
    """Returned when RAZORPAY_ENABLED is false. Every call is a hard stop."""

    async def create_payment_link(self, **_: Any) -> PaymentLinkResult:
        raise RazorpayDisabledError("RAZORPAY_ENABLED is false")

    async def fetch_payment_links_by_reference(self, _: str) -> list[PaymentLinkResult]:
        raise RazorpayDisabledError("RAZORPAY_ENABLED is false")

    def verify_webhook_signature(self, *, raw_body: bytes, signature: str) -> bool:
        raise RazorpayDisabledError("RAZORPAY_ENABLED is false")


def get_razorpay_client() -> RazorpayClient:
    """FastAPI dependency. Real SDK client when enabled, hard-stop client when not."""
    settings = get_settings()
    if not settings.razorpay_enabled:
        return DisabledRazorpayClient()
    return SdkRazorpayClient(
        key_id=settings.razorpay_key_id,
        key_secret=settings.razorpay_key_secret,
        webhook_secret=settings.razorpay_webhook_secret,
    )


def hmac_sha256_hex(secret: str, raw_body: bytes) -> str:
    """The exact signature Razorpay sends. Used by the test fake and available
    for tooling; production verification goes through `verify_webhook_signature`."""
    return hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
