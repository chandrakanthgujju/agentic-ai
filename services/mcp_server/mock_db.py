"""A *mock* data-access layer.

Nothing here talks to a database. Each method logs the parameterised SQL it
*would* run against a real store, sleeps for a realistic, configurable latency,
and answers from in-memory fixtures. Tools depend only on the ``CommerceRepository``
protocol, so swapping in a real Postgres/DynamoDB repository later is a one-class
change with no tool or agent changes.
"""

from __future__ import annotations

import logging
import os
import random
import time
from dataclasses import asdict, dataclass
from datetime import date
from typing import Protocol

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Customer:
    customer_id: str
    name: str
    email: str
    tier: str
    city: str
    country: str
    member_since: str
    lifetime_value_usd: float


@dataclass(frozen=True)
class OrderLine:
    sku: str
    quantity: int
    unit_price_usd: float


@dataclass(frozen=True)
class Order:
    order_id: str
    customer_id: str
    status: str
    placed_on: str
    carrier: str | None
    tracking_number: str | None
    estimated_delivery: str | None
    lines: tuple[OrderLine, ...]

    @property
    def total_usd(self) -> float:
        return round(sum(line.quantity * line.unit_price_usd for line in self.lines), 2)


@dataclass(frozen=True)
class Product:
    sku: str
    name: str
    category: str
    price_usd: float
    stock: int
    rating: float
    description: str


CUSTOMERS: dict[str, Customer] = {
    c.customer_id: c
    for c in [
        Customer("C001", "Asha Rao", "asha.rao@example.com", "gold", "Bengaluru", "IN", "2021-03-14", 4210.50),
        Customer("C002", "Daniel Kim", "daniel.kim@example.com", "silver", "Seattle", "US", "2022-07-01", 1180.00),
        Customer("C003", "Maria Gomez", "maria.gomez@example.com", "bronze", "Madrid", "ES", "2023-11-20", 240.75),
        Customer("C004", "Liam O'Brien", "liam.obrien@example.com", "gold", "Dublin", "IE", "2020-01-09", 7890.10),
        Customer("C005", "Yuki Tanaka", "yuki.tanaka@example.com", "silver", "Osaka", "JP", "2022-02-28", 1530.40),
    ]
}

PRODUCTS: dict[str, Product] = {
    p.sku: p
    for p in [
        Product("SKU-1001", "Trailblazer Running Shoe", "footwear", 89.99, 42, 4.5, "Lightweight trail shoe with grippy sole."),
        Product("SKU-1002", "CityWalk Sneaker", "footwear", 64.50, 0, 4.1, "Everyday sneaker, breathable mesh."),
        Product("SKU-2001", "Aero Rain Jacket", "apparel", 129.00, 15, 4.7, "Waterproof, packable shell jacket."),
        Product("SKU-2002", "Merino Base Layer", "apparel", 59.00, 80, 4.6, "Temperature-regulating merino wool top."),
        Product("SKU-3001", "Summit 30L Backpack", "gear", 110.00, 9, 4.8, "30 litre hiking pack with rain cover."),
        Product("SKU-3002", "Insulated Bottle 1L", "gear", 29.95, 120, 4.4, "Keeps drinks cold for 24 hours."),
        Product("SKU-3003", "Trekking Poles (pair)", "gear", 74.00, 3, 4.3, "Collapsible aluminium poles."),
    ]
}

ORDERS: dict[str, Order] = {
    o.order_id: o
    for o in [
        Order(
            "ORD-5001",
            "C001",
            "shipped",
            "2026-11-18",
            "BlueDart",
            "BD123456789IN",
            "2026-11-26",
            (OrderLine("SKU-1001", 1, 89.99), OrderLine("SKU-3002", 2, 29.95)),
        ),
        Order(
            "ORD-5002", "C001", "delivered", "2026-10-02", "Delhivery", "DL987654321IN", "2026-10-06", (OrderLine("SKU-2002", 2, 59.00),)
        ),
        Order("ORD-5003", "C002", "processing", "2026-11-22", None, None, "2026-11-29", (OrderLine("SKU-3001", 1, 110.00),)),
        Order("ORD-5004", "C003", "cancelled", "2026-09-15", None, None, None, (OrderLine("SKU-1002", 1, 64.50),)),
        Order(
            "ORD-5005",
            "C004",
            "out_for_delivery",
            "2026-11-20",
            "An Post",
            "AP555000111IE",
            "2026-11-24",
            (OrderLine("SKU-2001", 1, 129.00), OrderLine("SKU-3003", 1, 74.00)),
        ),
    ]
}


class CommerceRepository(Protocol):
    def get_customer(self, customer_id: str) -> Customer | None: ...
    def get_order(self, order_id: str) -> Order | None: ...
    def list_orders_for_customer(self, customer_id: str, limit: int) -> list[Order]: ...
    def search_products(
        self, query: str | None, category: str | None, max_price: float | None, in_stock_only: bool, limit: int
    ) -> list[Product]: ...


class MockCommerceRepository:
    """In-memory implementation that *pretends* to query a database."""

    def __init__(self, min_latency_ms: int | None = None, max_latency_ms: int | None = None, rng: random.Random | None = None):
        self.min_ms = int(os.environ.get("MOCK_DB_MIN_LATENCY_MS", 40) if min_latency_ms is None else min_latency_ms)
        self.max_ms = int(os.environ.get("MOCK_DB_MAX_LATENCY_MS", 180) if max_latency_ms is None else max_latency_ms)
        self._rng = rng or random.Random()
        self.queries: list[tuple[str, tuple]] = []  # visible for tests and demo logging

    def _simulate(self, sql: str, params: tuple) -> None:
        self.queries.append((sql, params))
        latency = self._rng.uniform(self.min_ms, self.max_ms) / 1000 if self.max_ms > 0 else 0
        log.info("mock-db query", extra={"sql": sql, "params": params, "simulated_latency_ms": round(latency * 1000)})
        if latency:
            time.sleep(latency)

    def get_customer(self, customer_id: str) -> Customer | None:
        self._simulate("SELECT * FROM customers WHERE customer_id = %s", (customer_id,))
        return CUSTOMERS.get(customer_id)

    def get_order(self, order_id: str) -> Order | None:
        self._simulate("SELECT * FROM orders o JOIN order_lines l USING (order_id) WHERE o.order_id = %s", (order_id,))
        return ORDERS.get(order_id)

    def list_orders_for_customer(self, customer_id: str, limit: int) -> list[Order]:
        self._simulate("SELECT * FROM orders WHERE customer_id = %s ORDER BY placed_on DESC LIMIT %s", (customer_id, limit))
        rows = [o for o in ORDERS.values() if o.customer_id == customer_id]
        return sorted(rows, key=lambda o: date.fromisoformat(o.placed_on), reverse=True)[:limit]

    def search_products(self, query, category, max_price, in_stock_only, limit) -> list[Product]:
        self._simulate(
            "SELECT * FROM products WHERE (name ILIKE %s OR description ILIKE %s) AND (%s IS NULL OR category = %s) "
            "AND (%s IS NULL OR price_usd <= %s) AND (NOT %s OR stock > 0) ORDER BY rating DESC LIMIT %s",
            (query, query, category, category, max_price, max_price, in_stock_only, limit),
        )
        q = (query or "").lower().strip()
        rows = [
            p
            for p in PRODUCTS.values()
            if (not q or q in p.name.lower() or q in p.description.lower() or q in p.category)
            and (category is None or p.category == category)
            and (max_price is None or p.price_usd <= max_price)
            and (not in_stock_only or p.stock > 0)
        ]
        return sorted(rows, key=lambda p: p.rating, reverse=True)[:limit]


def to_dict(obj) -> dict:
    d = asdict(obj)
    if isinstance(obj, Order):
        d["total_usd"] = obj.total_usd
    return d
