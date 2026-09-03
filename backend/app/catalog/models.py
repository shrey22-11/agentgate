"""
Catalog models — SIMULATED data.

The merchant, its products, stock levels, and margin figures are all synthetic
seed data (see `app/catalog/seed.py`). They are never presented as real
Razorpay or real merchant data. What is *not* simulated is how the policy
engine reads `price`, `max_discount_pct` and `min_margin_price` off these rows
to compute commercial boundaries — that logic is OUR SYSTEM and is exercised
for real.
"""
from __future__ import annotations

import uuid as _uuid
from decimal import Decimal

from sqlalchemy import ForeignKey, Numeric, String, Text, CheckConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, TimestampMixin, uuid_pk

# INR amounts: fixed-point, two decimal places, never float. All money in this
# codebase is Decimal end to end.
Money = Numeric(12, 2)
Percent = Numeric(5, 2)  # 0.00 .. 100.00


class Merchant(TimestampMixin, Base):
    __tablename__ = "merchant"

    id: Mapped[_uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)

    # The policy version currently in force for this merchant. Copied onto every
    # Decision so a decision can always be explained against the exact ruleset
    # that produced it, even after the policy later changes.
    policy_version: Mapped[str] = mapped_column(
        String(40), nullable=False, default="v1"
    )

    products: Mapped[list["Product"]] = relationship(
        back_populates="merchant", cascade="all, delete-orphan"
    )


class Product(TimestampMixin, Base):
    __tablename__ = "product"
    __table_args__ = (
        CheckConstraint("price >= 0", name="ck_product_price_nonneg"),
        CheckConstraint("stock >= 0", name="ck_product_stock_nonneg"),
        CheckConstraint(
            "max_discount_pct >= 0 AND max_discount_pct <= 100",
            name="ck_product_max_discount_pct_range",
        ),
        CheckConstraint(
            "min_margin_price >= 0 AND min_margin_price <= price",
            name="ck_product_margin_not_above_price",
        ),
    )

    id: Mapped[_uuid.UUID] = uuid_pk()
    merchant_id: Mapped[_uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("merchant.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(80), nullable=False, index=True)

    price: Mapped[Decimal] = mapped_column(Money, nullable=False)
    stock: Mapped[int] = mapped_column(nullable=False, default=0)

    # Commercial boundary inputs the policy engine reads. `max_discount_pct` is
    # the largest discount that may be auto-approved; `min_margin_price` is a
    # hard floor the final price may never cross, whatever the discount math says.
    max_discount_pct: Mapped[Decimal] = mapped_column(Percent, nullable=False)
    min_margin_price: Mapped[Decimal] = mapped_column(Money, nullable=False)

    merchant: Mapped["Merchant"] = relationship(back_populates="products")
