"""
Read-only catalogue views for the AI buyer agent.

`ProductView` is deliberately narrower than the `Product` row: it exposes what
an *external* buyer would see — name, category, price, availability — and hides
`max_discount_pct` and `min_margin_price`. The buyer agent is an untrusted
counterparty; it must discover a commercial boundary only by receiving a
`COUNTER_OFFER` back from the policy engine, never by reading the merchant's
ceiling out of the catalogue.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalog.models import Product


@dataclass(frozen=True)
class ProductView:
    product_id: uuid.UUID
    name: str
    description: str | None
    category: str
    price: Decimal
    stock: int

    def as_dict(self) -> dict:
        return {
            "product_id": str(self.product_id),
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "price": str(self.price),
            "in_stock": self.stock > 0,
            "stock": self.stock,
        }


def _view(product: Product) -> ProductView:
    return ProductView(
        product_id=product.id,
        name=product.name,
        description=product.description,
        category=product.category,
        price=product.price,
        stock=product.stock,
    )


async def search_catalog(
    session: AsyncSession,
    *,
    query: str | None = None,
    category: str | None = None,
    max_price_inr: Decimal | None = None,
    limit: int = 20,
) -> list[ProductView]:
    stmt = select(Product)
    if query:
        needle = query.strip().lower()
        stmt = stmt.where(
            func.lower(Product.name).contains(needle, autoescape=True)
            | func.lower(func.coalesce(Product.description, "")).contains(needle, autoescape=True)
            | func.lower(Product.category).contains(needle, autoescape=True)
        )
    if category:
        stmt = stmt.where(func.lower(Product.category) == category.strip().lower())
    if max_price_inr is not None:
        stmt = stmt.where(Product.price <= max_price_inr)
    stmt = stmt.order_by(Product.price.asc(), Product.name.asc()).limit(max(1, min(limit, 50)))
    return [_view(p) for p in (await session.scalars(stmt)).all()]


async def get_product(session: AsyncSession, product_id: uuid.UUID) -> ProductView | None:
    product = await session.get(Product, product_id)
    return _view(product) if product is not None else None


async def compare_products(
    session: AsyncSession, product_ids: list[uuid.UUID]
) -> list[ProductView]:
    if not product_ids:
        return []
    rows = (
        await session.scalars(
            select(Product).where(Product.id.in_(product_ids)).order_by(Product.price.asc())
        )
    ).all()
    return [_view(p) for p in rows]
