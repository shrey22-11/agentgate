"""Deterministic counter-offer / price-floor calculation (OUR SYSTEM)."""
from app.counter_offer.engine import CounterOffer, compute_floor

__all__ = ["CounterOffer", "compute_floor"]
