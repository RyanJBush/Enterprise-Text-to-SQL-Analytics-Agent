"""Approved schema: the single source of truth for what a query may touch.

The executor's authorizer allowlist AND the text shown to the model are both built
from this module, so the two can never drift apart. ``orders.internal_note`` is
deliberately absent from both.
"""

from __future__ import annotations

# table -> ((column, declared type), ...)
APPROVED_SCHEMA: dict[str, tuple[tuple[str, str], ...]] = {
    "products": (
        ("product_id", "INTEGER"),
        ("product_name", "TEXT"),
        ("category", "TEXT"),
    ),
    "orders": (
        ("order_id", "INTEGER"),
        ("order_date", "TEXT"),
        ("product_id", "INTEGER"),
        ("quantity", "INTEGER"),
        ("unit_price_cents", "INTEGER"),
        ("status", "TEXT"),
    ),
}

APPROVED_COLUMNS: dict[str, tuple[str, ...]] = {
    table: tuple(name for name, _ in columns) for table, columns in APPROVED_SCHEMA.items()
}

# Matched case-insensitively against the function name SQLite reports.
ALLOWED_FUNCTIONS: frozenset[str] = frozenset(
    {"count", "sum", "min", "max", "avg", "coalesce"}
)

BUSINESS_DEFINITIONS: tuple[str, ...] = (
    "Each order contains one product. Join orders.product_id to products.product_id.",
    "products.product_name identifies an individual named item; products.category groups items.",
    "Units sold means SUM(orders.quantity). Number of orders means COUNT(*).",
    "orders.status is 'paid' or 'cancelled'.",
    "order_date is the order's business calendar date in YYYY-MM-DD format.",
    "Date ranges use an inclusive start and an exclusive end "
    "(for January 2025: order_date >= '2025-01-01' AND order_date < '2025-02-01').",
    "Revenue is the sum of quantity * unit_price_cents over paid orders, "
    "using the price stored on the order.",
    "SQL results are integer USD cents; dollars are formatted only for display.",
)


def describe_schema() -> str:
    """The schema as shown to the model. Built only from APPROVED_SCHEMA."""
    lines = ["SQLite tables you may query:"]
    for table, columns in APPROVED_SCHEMA.items():
        lines.append(f"  {table}(" + ", ".join(f"{n} {t}" for n, t in columns) + ")")
    lines.append(
        "Write exactly one SELECT (or WITH ... SELECT) statement. "
        "Allowed functions: " + ", ".join(sorted(ALLOWED_FUNCTIONS)) + "."
    )
    return "\n".join(lines)
