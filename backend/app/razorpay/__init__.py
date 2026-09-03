"""
Razorpay execution boundary (REAL RAZORPAY, test mode).

The `razorpay` SDK is used only inside `app.razorpay.client`. Everything else
depends on the `RazorpayClient` protocol.
"""
from app.razorpay.client import (
    PaymentLinkResult,
    RazorpayClient,
    RazorpayDisabledError,
    RazorpayError,
    get_razorpay_client,
)
from app.razorpay.eligibility import ExecutionEligibility, can_execute

__all__ = [
    "PaymentLinkResult",
    "RazorpayClient",
    "RazorpayError",
    "RazorpayDisabledError",
    "get_razorpay_client",
    "can_execute",
    "ExecutionEligibility",
]
