"""Short answers derived from returned rows.

Every number and label in an answer comes from the rows. The only text that does not is
the optional ``context`` ("In January 2025, paid"), which the caller supplies because rows
cannot know the period or the filter.
"""

from __future__ import annotations

from collections.abc import Sequence

NO_ROWS = "No matching orders."


def format_cents(cents: int) -> str:
    """Integer USD cents -> dollars, using integer arithmetic only."""
    sign = "-" if cents < 0 else ""
    dollars, remainder = divmod(abs(cents), 100)
    return f"{sign}${dollars:,}.{remainder:02d}"


def format_value(column: str, value: object) -> str:
    if value is None:
        return "NULL"
    if column.endswith("_cents") and isinstance(value, int) and not isinstance(value, bool):
        return format_cents(value)
    return str(value)


def humanize(column: str) -> str:
    return column.removesuffix("_cents").replace("_", " ")


def _join(parts: list[str]) -> str:
    if len(parts) <= 2:
        return " and ".join(parts)
    return ", ".join(parts[:-1]) + ", and " + parts[-1]


def summarize(columns: Sequence[str], rows: Sequence[Sequence[object]], context: str = "") -> str:
    if not rows:
        return NO_ROWS
    lead = f"{context} " if context else ""

    if len(columns) == 1:
        values = ", ".join(format_value(columns[0], row[0]) for row in rows)
        return f"{lead}{humanize(columns[0])}: {values}."

    if len(columns) == 2 and all(
        isinstance(r[0], str) and isinstance(r[1], int) and not isinstance(r[1], bool)
        for r in rows
    ):
        parts = [f"{format_value(columns[1], r[1])} for {r[0]}" for r in rows]
        return f"{lead}{humanize(columns[1])} was {_join(parts)}."

    noun = "row" if len(rows) == 1 else "rows"
    return f"{len(rows)} {noun} returned; see the table."
