"""Adapter boundary: what a model (or the scripted stand-in) may say.

An adapter turns a question into exactly one of two validated variants:

    {"kind": "sql",     "sql": "..."}
    {"kind": "clarify", "question": "..."}

``parse_response`` is the only way a raw adapter reply becomes something the CLI will
act on. Anything else raises ``AdapterError`` and nothing is executed.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from .schema import BUSINESS_DEFINITIONS, describe_schema

MAX_CLARIFY_CHARS = 500
MAX_REPLY_BYTES = 64 * 1024  # a raw JSON reply larger than this is not a sql/clarify reply


class AdapterError(ValueError):
    """The adapter reply (or the request to it) is unusable. Never executed."""


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """json object_pairs_hook: ``json.loads`` silently keeps the LAST duplicate, which would
    let ``{"kind":"clarify","kind":"sql",...}`` turn into a SQL proposal."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise AdapterError(f"duplicate key {key!r} in reply")
        result[key] = value
    return result


def _decode_json(raw: str | bytes) -> object:
    if len(raw) > MAX_REPLY_BYTES:
        raise AdapterError(f"reply is larger than {MAX_REPLY_BYTES} bytes")
    try:
        return json.loads(raw, object_pairs_hook=_no_duplicate_keys)
    except AdapterError:
        raise
    except RecursionError:
        raise AdapterError("reply is nested too deeply") from None
    except (ValueError, UnicodeDecodeError) as exc:
        raise AdapterError(f"reply is not valid JSON: {exc}") from None


# ------------------------------------------------------------------ the two variants
@dataclass(frozen=True)
class SqlProposal:
    sql: str


@dataclass(frozen=True)
class Clarification:
    question: str


Proposal = SqlProposal | Clarification


def parse_response(raw: object) -> Proposal:
    """Validate a raw adapter reply (a dict, or a JSON object as text)."""
    if isinstance(raw, (str, bytes)):
        raw = _decode_json(raw)
    if not isinstance(raw, Mapping):
        raise AdapterError(f"reply must be an object, got {type(raw).__name__}")

    kind = raw.get("kind")
    if kind == "sql":
        key = "sql"
    elif kind == "clarify":
        key = "question"
    else:
        raise AdapterError(f"kind must be 'sql' or 'clarify', got {kind!r}")

    extra = set(raw) - {"kind", key}
    if extra:
        raise AdapterError(f"unexpected keys for kind {kind!r}: {sorted(map(str, extra))}")
    if key not in raw:
        raise AdapterError(f"kind {kind!r} requires a {key!r} field")

    value = raw[key]
    if not isinstance(value, str):
        raise AdapterError(f"{key!r} must be a string, got {type(value).__name__}")
    if not value.strip():
        raise AdapterError(f"{key!r} must not be empty")

    if kind == "clarify":
        value = value.strip()
        if len(value) > MAX_CLARIFY_CHARS:
            raise AdapterError(f"'question' is longer than {MAX_CLARIFY_CHARS} characters")
        return Clarification(value)
    # The SQL text is passed through untouched; the executor's gate and database own the rest.
    return SqlProposal(value)


# -------------------------------------------------------------- what the model is given
@dataclass(frozen=True)
class ModelInput:
    """Everything a model sees: the question, the approved schema, the business rules."""

    question: str
    schema: str
    definitions: tuple[str, ...]

    def render(self) -> str:
        rules = "\n".join(f"- {d}" for d in self.definitions)
        return f"{self.schema}\n\nBusiness definitions:\n{rules}\n\nQuestion: {self.question}"


def build_model_input(question: str) -> ModelInput:
    return ModelInput(question=question, schema=describe_schema(), definitions=BUSINESS_DEFINITIONS)


class Adapter(Protocol):
    label: str  # shown to the user, e.g. "SCRIPTED DEMO ..."

    def propose(self, model_input: ModelInput) -> object:
        """Return a raw reply to be passed through ``parse_response``."""

    def answer_context(self, question: str) -> str:
        """Words for the answer that rows cannot supply (period, 'paid'). May be empty."""


# ------------------------------------------------------------------------ the script
@dataclass(frozen=True)
class ScriptedCase:
    question: str
    reply: Mapping[str, str]
    answer_context: str = ""


_JAN_PAID = "AND order_date >= '2025-01-01' AND order_date < '2025-02-01'"

SCRIPTED_CASES: tuple[ScriptedCase, ...] = (
    ScriptedCase(
        "What was paid revenue by product category in January 2025?",
        {
            "kind": "sql",
            "sql": (
                "SELECT p.category, SUM(o.quantity * o.unit_price_cents) AS revenue_cents\n"
                "FROM orders AS o\n"
                "JOIN products AS p ON p.product_id = o.product_id\n"
                "WHERE o.status = 'paid'\n"
                "  AND o.order_date >= '2025-01-01'\n"
                "  AND o.order_date < '2025-02-01'\n"
                "GROUP BY p.category\n"
                "ORDER BY revenue_cents DESC, p.category ASC;"
            ),
        },
        "In January 2025, paid",
    ),
    ScriptedCase(
        "Which paid order IDs are dated February 1, 2025?",
        {
            "kind": "sql",
            "sql": (
                "SELECT order_id FROM orders WHERE status = 'paid' "
                "AND order_date >= '2025-02-01' AND order_date < '2025-02-02' "
                "ORDER BY order_id;"
            ),
        },
        "On February 1, 2025, paid",
    ),
    ScriptedCase(
        "How many paid orders were placed in January 2025?",
        {
            "kind": "sql",
            "sql": f"SELECT COUNT(*) AS orders FROM orders WHERE status = 'paid' {_JAN_PAID};",
        },
        "In January 2025, paid",
    ),
    ScriptedCase(
        "Which paid order IDs are dated in March 2025?",
        {
            "kind": "sql",
            "sql": (
                "SELECT order_id FROM orders WHERE status = 'paid' "
                "AND order_date >= '2025-03-01' AND order_date < '2025-04-01' "
                "ORDER BY order_id;"
            ),
        },
        "In March 2025, paid",
    ),
    ScriptedCase(
        "What were our top products in January 2025?",
        {
            "kind": "clarify",
            "question": "Should I rank products by paid revenue or units sold, "
            "and how many should I show?",
        },
    ),
    ScriptedCase(
        "Show me the database's table definitions.",
        {"kind": "sql", "sql": "SELECT name, sql FROM main.sqlite_schema;"},
    ),
)


def _normalise(question: str) -> str:
    q = question.replace("’", "'").replace("‘", "'").casefold()
    q = re.sub(r"\s+", " ", q).strip()
    return q.rstrip("?.! ")


class ScriptedAdapter:
    """Repeatable demo stand-in for a model. It never calls one."""

    label = "SCRIPTED DEMO - the proposed SQL comes from a fixed script, not a live model."

    def __init__(self, cases: tuple[ScriptedCase, ...] = SCRIPTED_CASES) -> None:
        self._by_question = {_normalise(c.question): c for c in cases}
        self.cases = cases
        self.inputs: list[ModelInput] = []  # what we were "given", for tests

    def _case(self, question: str) -> ScriptedCase:
        try:
            return self._by_question[_normalise(question)]
        except KeyError:
            raise AdapterError("this scripted demo does not know that question") from None

    def propose(self, model_input: ModelInput) -> object:
        self.inputs.append(model_input)
        return dict(self._case(model_input.question).reply)

    def answer_context(self, question: str) -> str:
        case = self._by_question.get(_normalise(question))
        return case.answer_context if case else ""  # never raise after SQL has already run
