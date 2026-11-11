"""Adapter validation, answers, and the CLI end to end (scripted adapter only)."""

from __future__ import annotations

import contextlib
import io
import os
import sqlite3
import subprocess
import sys
import unicodedata
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import FixtureTestCase
from text2sql import cli
from text2sql.adapter import (
    SCRIPTED_CASES,
    AdapterError,
    Clarification,
    ScriptedAdapter,
    SqlProposal,
    build_model_input,
    parse_response,
)
from text2sql.answer import format_cents, summarize
from text2sql.executor import Status, check_input, execute_query
from text2sql.schema import ALLOWED_FUNCTIONS, APPROVED_COLUMNS, describe_schema

ROOT = Path(__file__).resolve().parent.parent

Q_JOINED = "What was paid revenue by product category in January 2025?"
Q_FEB1 = "Which paid order IDs are dated February 1, 2025?"
Q_COUNT = "How many paid orders were placed in January 2025?"
Q_MARCH = "Which paid order IDs are dated in March 2025?"
Q_AMBIGUOUS = "What were our top products in January 2025?"
Q_FORBIDDEN = "Show me the database's table definitions."

LABEL = "SCRIPTED DEMO - the proposed SQL comes from a fixed script, not a live model."

# The three reviewed transcripts, character for character.
TRANSCRIPT_JOINED = f"""{LABEL}
Question: {Q_JOINED}

Proposed SQL:
  SELECT p.category, SUM(o.quantity * o.unit_price_cents) AS revenue_cents
  FROM orders AS o
  JOIN products AS p ON p.product_id = o.product_id
  WHERE o.status = 'paid'
    AND o.order_date >= '2025-01-01'
    AND o.order_date < '2025-02-01'
  GROUP BY p.category
  ORDER BY revenue_cents DESC, p.category ASC;

Status: ok - Query ran.
Rows (2):
  category | revenue_cents
  ---------+--------------
  Hardware | 13000
  Supplies | 6500

Answer: In January 2025, paid revenue was $130.00 for Hardware and $65.00 for Supplies.
"""

TRANSCRIPT_AMBIGUOUS = f"""{LABEL}
Question: {Q_AMBIGUOUS}

Clarification needed. No SQL was run.
  Should I rank products by paid revenue or units sold, and how many should I show?
"""

TRANSCRIPT_FORBIDDEN = f"""{LABEL}
Question: {Q_FORBIDDEN}

Proposed SQL:
  SELECT name, sql FROM main.sqlite_schema;

Status: denied - blocked by the database (SQLITE_READ on sqlite_master.name). No rows were returned.
"""

CROSS7 = "SELECT count(*) FROM " + ", ".join(f"orders AS t{i}" for i in range(7))


class ExecutorSpy:
    """Wraps the real executor and records every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, sql, **kwargs):
        self.calls.append((sql, kwargs))
        return execute_query(sql, **kwargs)


class FixedAdapter:
    label = "FIXED TEST ADAPTER"

    def __init__(self, reply: object) -> None:
        self.reply = reply

    def propose(self, model_input):
        return self.reply

    def answer_context(self, question: str) -> str:
        return ""


# ================================================================ reply validation
class ParseResponse(unittest.TestCase):
    def test_valid_sql_variant_is_passed_through_untouched(self):
        sql = "  SELECT 1;\n"
        self.assertEqual(parse_response({"kind": "sql", "sql": sql}), SqlProposal(sql))

    def test_valid_clarify_variant_is_trimmed(self):
        self.assertEqual(
            parse_response({"kind": "clarify", "question": "  Which one?  "}),
            Clarification("Which one?"),
        )

    def test_json_text_and_bytes_are_accepted(self):
        for raw in ('{"kind":"sql","sql":"SELECT 1"}', b'{"kind":"sql","sql":"SELECT 1"}'):
            with self.subTest(raw=raw):
                self.assertEqual(parse_response(raw), SqlProposal("SELECT 1"))

    BAD = [
        None, 42, [], ["sql", "SELECT 1"], "", "not json", "[]", '"SELECT 1"', "{",
        b"\xff\xfe",
        {}, {"sql": "SELECT 1"}, {"kind": "sql"}, {"kind": "clarify"},
        {"kind": "other", "sql": "SELECT 1"}, {"kind": "SQL", "sql": "SELECT 1"},
        {"kind": None, "sql": "SELECT 1"},
        {"kind": "sql", "sql": "SELECT 1", "question": "also?"},  # both variants at once
        {"kind": "sql", "sql": "SELECT 1", "extra": 1},
        {"kind": "sql", "question": "Which?"},  # wrong field for the kind
        {"kind": "clarify", "sql": "SELECT 1"},
        {"kind": "clarify", "question": "Which?", "sql": "SELECT 1"},
        {"kind": "sql", "sql": ""}, {"kind": "sql", "sql": "   \n"},
        {"kind": "sql", "sql": None}, {"kind": "sql", "sql": 1},
        {"kind": "sql", "sql": ["SELECT 1"]}, {"kind": "sql", "sql": b"SELECT 1"},
        {"kind": "clarify", "question": ""}, {"kind": "clarify", "question": "  "},
        {"kind": "clarify", "question": 5}, {"kind": "clarify", "question": "x" * 501},
        # duplicate JSON keys: json.loads would silently keep the last one
        '{"kind":"clarify","kind":"sql","sql":"SELECT 1"}',
        '{"kind":"sql","kind":"clarify","question":"Which?"}',
        '{"kind":"sql","sql":"SELECT 1","sql":"DROP TABLE orders"}',
        '{"kind":"sql","kind":"sql","sql":"SELECT 1"}',  # even when the values agree
        b'{"kind":"sql","sql":"SELECT 1","sql":"SELECT 1"}',
        # untrusted text must not crash the parser
        "[" * 50_000,
        '{"kind":"sql","sql":"' + "x" * 70_000 + '"}',
    ]

    def test_everything_else_is_rejected(self):
        for raw in self.BAD:
            with self.subTest(raw=raw if not isinstance(raw, dict) else str(raw)[:80]):
                with self.assertRaises(AdapterError):
                    parse_response(raw)

    def test_duplicate_keys_are_named_in_the_error(self):
        with self.assertRaisesRegex(AdapterError, r"duplicate key 'kind'"):
            parse_response('{"kind":"clarify","kind":"sql","sql":"SELECT 1"}')
        with self.assertRaisesRegex(AdapterError, r"duplicate key 'sql'"):
            parse_response('{"kind":"sql","sql":"SELECT 1","sql":"SELECT 2"}')

    def test_parser_limits_are_reported_as_such(self):
        with self.assertRaisesRegex(AdapterError, "nested too deeply"):
            parse_response("[" * 50_000)
        with self.assertRaisesRegex(AdapterError, "larger than"):
            parse_response('{"kind":"sql","sql":"' + "x" * 70_000 + '"}')

    def test_distinct_keys_in_either_order_are_still_fine(self):
        self.assertEqual(parse_response('{"sql":"SELECT 1","kind":"sql"}'), SqlProposal("SELECT 1"))

    def test_clarify_at_the_length_cap_is_fine(self):
        self.assertEqual(len(parse_response({"kind": "clarify", "question": "x" * 500}).question), 500)


# ================================================================ scripted adapter
class ScriptedAdapterTests(unittest.TestCase):
    def test_every_scripted_reply_is_valid_and_sql_replies_pass_the_gate(self):
        adapter = ScriptedAdapter()
        for case in SCRIPTED_CASES:
            with self.subTest(case.question):
                proposal = parse_response(adapter.propose(build_model_input(case.question)))
                if isinstance(proposal, SqlProposal):
                    self.assertIsNone(check_input(proposal.sql))

    def test_there_are_exactly_the_six_handoff_questions(self):
        self.assertEqual(
            [c.question for c in SCRIPTED_CASES],
            [Q_JOINED, Q_FEB1, Q_COUNT, Q_MARCH, Q_AMBIGUOUS, Q_FORBIDDEN],
        )

    def test_matching_ignores_case_spacing_trailing_punctuation_and_curly_quotes(self):
        adapter = ScriptedAdapter()
        for q in ("show me the DATABASE’s   table definitions", "Show me the database's table definitions?"):
            with self.subTest(q=q):
                reply = adapter.propose(build_model_input(q))
                self.assertEqual(reply["sql"], "SELECT name, sql FROM main.sqlite_schema;")

    def test_unknown_question_is_an_adapter_error(self):
        with self.assertRaises(AdapterError):
            ScriptedAdapter().propose(build_model_input("What is the airspeed of a swallow?"))

    def test_replies_are_copies_so_callers_cannot_edit_the_script(self):
        adapter = ScriptedAdapter()
        first = adapter.propose(build_model_input(Q_JOINED))
        first["sql"] = "DROP TABLE orders"
        self.assertNotIn("DROP", adapter.propose(build_model_input(Q_JOINED))["sql"])


class ModelInputTests(unittest.TestCase):
    def test_model_sees_question_approved_schema_and_business_definitions(self):
        text = build_model_input(Q_JOINED).render()
        self.assertIn(Q_JOINED, text)
        for table, columns in APPROVED_COLUMNS.items():
            self.assertIn(f"{table}(", text)
            for column in columns:
                self.assertIn(column, text)
        for fn in ALLOWED_FUNCTIONS:
            self.assertIn(fn, text)
        for phrase in ("inclusive start", "exclusive end", "quantity * unit_price_cents",
                       "paid", "integer USD cents", "orders.product_id to products.product_id"):
            self.assertIn(phrase, text)

    def test_private_column_never_reaches_the_model(self):
        self.assertNotIn("internal_note", build_model_input(Q_JOINED).render())
        self.assertNotIn("internal_note", describe_schema())


# ====================================================================== answers
class AnswerTests(unittest.TestCase):
    def test_cents_become_dollars_with_integer_arithmetic(self):
        cases = {13000: "$130.00", 6500: "$65.00", 5: "$0.05", 0: "$0.00",
                 123456789: "$1,234,567.89", -250: "-$2.50", 99: "$0.99", 100: "$1.00"}
        for cents, text in cases.items():
            with self.subTest(cents=cents):
                self.assertEqual(format_cents(cents), text)

    def test_joined_answer_matches_the_handoff_sentence(self):
        self.assertEqual(
            summarize(("category", "revenue_cents"), [("Hardware", 13000), ("Supplies", 6500)],
                      "In January 2025, paid"),
            "In January 2025, paid revenue was $130.00 for Hardware and $65.00 for Supplies.",
        )

    def test_answer_follows_the_rows_not_a_script(self):
        columns = ("category", "revenue_cents")
        self.assertEqual(summarize(columns, [("Books", 12345)]), "revenue was $123.45 for Books.")
        self.assertEqual(
            summarize(columns, [("A", 100), ("B", 250), ("C", 7)]),
            "revenue was $1.00 for A, $2.50 for B, and $0.07 for C.",
        )

    def test_empty_rows(self):
        self.assertEqual(summarize(("order_id",), []), "No matching orders.")

    def test_single_column_answers(self):
        self.assertEqual(summarize(("order_id",), [(107,)], "On February 1, 2025, paid"),
                         "On February 1, 2025, paid order id: 107.")
        self.assertEqual(summarize(("orders",), [(5,)], "In January 2025, paid"),
                         "In January 2025, paid orders: 5.")
        self.assertEqual(summarize(("order_id",), [(1,), (2,)]), "order id: 1, 2.")
        self.assertEqual(summarize(("note",), [(None,)]), "note: NULL.")

    def test_other_shapes_fall_back_without_inventing_numbers(self):
        self.assertEqual(summarize(("a", "b", "c"), [(1, 2, 3)]), "1 row returned; see the table.")
        self.assertEqual(summarize(("a", "b", "c"), [(1, 2, 3)] * 2), "2 rows returned; see the table.")


# ============================================================= the CLI, end to end
class CliBase(FixtureTestCase):
    def run_cli(self, question=None, *, adapter=None, execute=None, extra=()):
        argv = ([question] if question is not None else []) + ["--db", str(self.db_path), *extra]
        out = io.StringIO()
        kwargs = {"out": out, "adapter": adapter}
        if execute is not None:
            kwargs["execute"] = execute
        code = cli.main(argv, **kwargs)
        return code, out.getvalue()

    def run_subprocess(self, question):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
        proc = subprocess.run(
            [sys.executable, "-X", "dev", "-W", "error", "-m", "text2sql.cli",
             "--db", str(self.db_path), question],
            cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        return proc


class CliThreePaths(CliBase):
    """The three reviewed paths, through the real `python -m text2sql.cli` entry point."""

    def test_january_revenue_join(self):
        proc = self.run_subprocess(Q_JOINED)
        self.assertEqual((proc.returncode, proc.stderr), (0, ""))
        self.assertEqual(proc.stdout, TRANSCRIPT_JOINED)

    def test_ambiguous_top_products_asks_and_runs_nothing(self):
        proc = self.run_subprocess(Q_AMBIGUOUS)
        self.assertEqual((proc.returncode, proc.stderr), (0, ""))
        self.assertEqual(proc.stdout, TRANSCRIPT_AMBIGUOUS)

    def test_forbidden_schema_read_shows_sql_and_blocked_status(self):
        proc = self.run_subprocess(Q_FORBIDDEN)
        self.assertEqual((proc.returncode, proc.stderr), (1, ""))
        self.assertEqual(proc.stdout, TRANSCRIPT_FORBIDDEN)

    def test_same_transcripts_in_process(self):
        for question, code, transcript in (
            (Q_JOINED, 0, TRANSCRIPT_JOINED),
            (Q_AMBIGUOUS, 0, TRANSCRIPT_AMBIGUOUS),
            (Q_FORBIDDEN, 1, TRANSCRIPT_FORBIDDEN),
        ):
            with self.subTest(question):
                self.assertEqual(self.run_cli(question), (code, transcript))


class CliClarification(CliBase):
    def test_clarify_calls_the_executor_zero_times_and_never_touches_the_database(self):
        spy, adapter = ExecutorSpy(), ScriptedAdapter()
        with mock.patch.object(sqlite3, "connect", side_effect=AssertionError("database touched")):
            code, text = self.run_cli(Q_AMBIGUOUS, adapter=adapter, execute=spy)
        self.assertEqual(code, 0)
        self.assertEqual(spy.calls, [])
        self.assertIn("Should I rank products by paid revenue or units sold", text)
        self.assertNotIn("Proposed SQL", text)
        self.assertNotIn("Status:", text)
        self.assertEqual(len(adapter.inputs), 1, "the adapter is asked once")

    def test_model_input_given_to_the_adapter_is_the_approved_one(self):
        adapter = ScriptedAdapter()
        self.run_cli(Q_AMBIGUOUS, adapter=adapter)
        (given,) = adapter.inputs
        self.assertEqual(given.question, Q_AMBIGUOUS)
        self.assertEqual(given.schema, describe_schema())
        self.assertNotIn("internal_note", given.render())


class CliSuccessAndDenial(CliBase):
    def test_success_calls_the_executor_once_with_the_proposed_sql_and_absolute_path(self):
        spy = ExecutorSpy()
        self.run_cli(Q_JOINED, execute=spy)
        ((sql, kwargs),) = spy.calls
        self.assertEqual(sql, SCRIPTED_CASES[0].reply["sql"])
        self.assertEqual(kwargs, {"db_path": self.db_path})
        self.assertTrue(kwargs["db_path"].is_absolute())

    def test_relative_db_argument_is_made_absolute_before_the_executor_sees_it(self):
        previous = os.getcwd()
        os.chdir(self.db_path.parent)
        self.addCleanup(os.chdir, previous)
        spy, out = ExecutorSpy(), io.StringIO()
        code = cli.main([Q_COUNT, "--db", self.db_path.name], out=out, execute=spy)
        self.assertEqual(code, 0, out.getvalue())
        ((_, kwargs),) = spy.calls
        self.assertTrue(kwargs["db_path"].is_absolute())
        self.assertEqual(kwargs["db_path"].resolve(), self.db_path.resolve())

    def test_denial_shows_the_sql_and_status_but_no_rows_or_answer(self):
        code, text = self.run_cli(Q_FORBIDDEN)
        self.assertEqual(code, 1)
        self.assertIn("SELECT name, sql FROM main.sqlite_schema;", text)
        self.assertIn("Status: denied", text)
        self.assertNotIn("Rows", text)
        self.assertNotIn("Answer", text)

    SIX = [
        (Q_JOINED, 0, "Answer: In January 2025, paid revenue was $130.00 for Hardware and $65.00 for Supplies."),
        (Q_FEB1, 0, "Answer: On February 1, 2025, paid order id: 107."),
        (Q_COUNT, 0, "Answer: In January 2025, paid orders: 5."),
        (Q_MARCH, 0, "Answer: No matching orders."),
        (Q_AMBIGUOUS, 0, "Clarification needed. No SQL was run."),
        (Q_FORBIDDEN, 1, "Status: denied"),
    ]

    def test_all_six_scripted_questions(self):
        for question, code, expected in self.SIX:
            with self.subTest(question):
                got_code, text = self.run_cli(question)
                self.assertEqual(got_code, code)
                self.assertIn(expected, text)
                self.assertTrue(text.startswith(LABEL), "every transcript is labelled scripted")

    def test_raw_rows_stay_in_cents_and_the_answer_is_in_dollars(self):
        _, text = self.run_cli(Q_JOINED)
        self.assertIn("Hardware | 13000", text)
        self.assertIn("$130.00", text)

    def test_empty_result_prints_no_rows_and_the_empty_answer(self):
        code, text = self.run_cli(Q_MARCH)
        self.assertEqual(code, 0)
        self.assertIn("Rows (0): none", text)
        self.assertTrue(text.endswith("Answer: No matching orders.\n"))

    def test_other_database_refusals_show_status_and_exit_1(self):
        cases = {
            "limit_work": CROSS7,
            "rejected": "PRAGMA table_info(orders)",
            "invalid_sql": "SELECT nope FROM orders",
            "denied": "WITH x AS (SELECT 1) DELETE FROM orders",
        }
        for status, sql in cases.items():
            with self.subTest(status):
                code, text = self.run_cli("anything", adapter=FixedAdapter({"kind": "sql", "sql": sql}))
                self.assertEqual(code, 1)
                self.assertIn(f"Status: {status}", text)
                self.assertIn(sql.splitlines()[0], text, "the proposed SQL is always shown")
                self.assertNotIn("Answer", text)
        _, denied = self.run_cli("x", adapter=FixedAdapter({"kind": "sql", "sql": cases["denied"]}))
        self.assertIn("SQLITE_DELETE on orders", denied)


class CliRefusesBadAdapterReplies(CliBase):
    def test_invalid_replies_never_reach_the_executor(self):
        for raw in ParseResponse.BAD:
            with self.subTest(raw=str(raw)[:60]):
                spy = ExecutorSpy()
                with mock.patch.object(sqlite3, "connect", side_effect=AssertionError("database touched")):
                    code, text = self.run_cli("anything", adapter=FixedAdapter(raw), execute=spy)
                self.assertEqual(code, 2)
                self.assertEqual(spy.calls, [])
                self.assertIn("Adapter reply rejected", text)
                self.assertIn("Nothing was executed", text)

    def test_a_valid_but_hostile_sql_reply_is_stopped_by_the_executor_not_by_us(self):
        spy = ExecutorSpy()
        code, text = self.run_cli(
            "anything", adapter=FixedAdapter({"kind": "sql", "sql": "SELECT 1; DROP TABLE orders"}), execute=spy
        )
        self.assertEqual((code, len(spy.calls)), (1, 1))
        self.assertIn("Status: invalid_sql", text)


class CliDuplicateKeys(CliBase):
    def test_duplicate_key_reply_is_rejected_and_the_executor_is_called_zero_times(self):
        reply = '{"kind":"clarify","kind":"sql","sql":"SELECT 1"}'
        spy = ExecutorSpy()
        with mock.patch.object(sqlite3, "connect", side_effect=AssertionError("database touched")):
            code, text = self.run_cli("anything", adapter=FixedAdapter(reply), execute=spy)
        self.assertEqual(code, 2)
        self.assertEqual(spy.calls, [])
        self.assertIn("Adapter reply rejected: duplicate key 'kind'", text)
        self.assertIn("Nothing was executed", text)
        self.assertNotIn("Proposed SQL", text)


CONTROL_CATEGORIES = {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}


def raw_controls(text: str) -> list[str]:
    """Characters that would act on a terminal (newline is our own line structure)."""
    return [c for c in text if c != "\n" and unicodedata.category(c) in CONTROL_CATEGORIES]


class CliTerminalSafety(CliBase):
    CLEAR = "\x1b[2J\x1b[H"  # ANSI: clear screen, cursor home

    def sql_run(self, sql, question="anything"):
        spy = ExecutorSpy()
        code, text = self.run_cli(question, adapter=FixedAdapter({"kind": "sql", "sql": sql}), execute=spy)
        return code, text, spy

    def test_ansi_sequence_in_a_sql_comment_is_shown_not_obeyed_and_sqlite_gets_the_original(self):
        sql = f"SELECT 1 /* {self.CLEAR} */ AS x"
        code, text, spy = self.sql_run(sql)
        self.assertEqual(code, 0, text)
        self.assertEqual(raw_controls(text), [], "no raw control character may reach stdout")
        self.assertNotIn("\x1b", text)
        self.assertIn("\\x1b[2J\\x1b[H", text, "displayed as visible text")
        ((executed, _),) = spy.calls
        self.assertEqual(executed, sql, "SQLite must receive the original, unescaped SQL")
        self.assertIn("\x1b", executed)

    def test_ansi_sequence_in_the_same_sql_through_the_real_subprocess(self):
        # Same attack, real entry point, real stdout bytes.
        reply = f"SELECT 1 /* {self.CLEAR} */ AS x"
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
        code = (
            "import sys; from text2sql import cli; from text2sql.adapter import ScriptedAdapter as S\n"
            "class A(S):\n"
            "    def propose(self, mi): return {'kind': 'sql', 'sql': sys.argv[3]}\n"
            "raise SystemExit(cli.main([sys.argv[1], '--db', sys.argv[2]], adapter=A()))\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code, "q", str(self.db_path), reply],
            cwd=ROOT, env=env, capture_output=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn(b"\x1b", proc.stdout, "raw ESC byte reached stdout")
        self.assertIn(b"\\x1b[2J\\x1b[H", proc.stdout)

    def test_question_is_escaped(self):
        _, text, spy = self.sql_run("SELECT 1", question=f"hi {self.CLEAR}‮")
        self.assertEqual(raw_controls(text), [])
        self.assertIn("Question: hi \\x1b[2J\\x1b[H\\u202e", text)

    def test_clarification_is_escaped(self):
        spy = ExecutorSpy()
        reply = {"kind": "clarify", "question": f"Which?\x1b[31m red\x07   ‮"}
        code, text = self.run_cli("anything", adapter=FixedAdapter(reply), execute=spy)
        self.assertEqual((code, spy.calls), (0, []))
        self.assertEqual(raw_controls(text), [])
        self.assertIn("Which?\\x1b[31m red\\x07 \\u2028 \\u202e", text)

    def test_column_names_and_row_values_are_escaped_and_the_table_stays_aligned(self):
        sql = "SELECT 'a\x1b[31mb' AS \"c\x1b[2Jd\", 'x\r\ny\tz' AS plain"
        code, text, spy = self.sql_run(sql)
        self.assertEqual(code, 0, text)
        self.assertEqual(raw_controls(text), [])
        self.assertIn("c\\x1b[2Jd", text)
        self.assertIn("a\\x1b[31mb", text)
        self.assertIn("x\\r\\ny\\tz", text)
        rows = text.split("Rows (1):\n")[1].split("\n\n")[0].split("\n")
        self.assertEqual(len(rows), 3, "header, rule, one row: embedded newlines must not add lines")
        pipe_columns = {line.index("|") for line in rows if "|" in line}
        self.assertEqual(len(pipe_columns), 1, "columns line up after escaping")

    def test_database_diagnostics_are_escaped(self):
        code, text, spy = self.sql_run(f"SELECT [{self.CLEAR}] FROM orders")
        self.assertEqual(code, 1)
        self.assertIn("Status: invalid_sql", text)
        self.assertEqual(raw_controls(text), [])
        self.assertIn("\\x1b[2J", text.split("Detail:")[1])

    def test_carriage_return_and_bidi_in_sql_are_visible_and_do_not_hide_lines(self):
        sql = "SELECT 1\r\n-- ‮ evil\n"
        code, text, spy = self.sql_run(sql)
        self.assertEqual(raw_controls(text), [])
        self.assertIn("  SELECT 1\\r", text)
        self.assertIn("-- \\u202e evil", text)
        self.assertEqual(spy.calls[0][0], sql)

    def test_names_echoed_back_by_the_database_are_escaped(self):
        code, text, spy = self.sql_run("SELECT * FROM [\x1b[2Jnope]")
        self.assertEqual(code, 1)
        self.assertEqual(raw_controls(text), [])
        self.assertIn("no such table: \\x1b[2Jnope", text)

    def test_non_ascii_output_survives_an_ascii_terminal(self):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="ascii")
        proc = subprocess.run(
            [sys.executable, "-m", "text2sql.cli", "--db", str(self.db_path), "café 中"],
            cwd=ROOT, env=env, capture_output=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stderr, b"")
        self.assertIn(b"Question: caf\\xe9 \\u4e2d", proc.stdout)


class CliParserDiagnostics(CliBase):
    """argparse prints option text straight to stderr; it must be escaped like everything else."""

    BAD_OPTION = "--bad\x1b[2J\x1b[H"

    def run_raw(self, *args):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
        return subprocess.run(
            [sys.executable, "-X", "dev", "-W", "error", "-m", "text2sql.cli", *args],
            cwd=ROOT, env=env, capture_output=True, timeout=60,
        )

    def test_invalid_option_with_ansi_sequence_exits_2_and_stderr_has_no_raw_escape(self):
        proc = self.run_raw(self.BAD_OPTION)
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn(b"\x1b", proc.stderr, "raw ESC byte reached stderr")
        self.assertIn(b"--bad\\x1b[2J\\x1b[H", proc.stderr, "shown as visible text")
        self.assertEqual(proc.stdout, b"")

    def test_no_raw_control_bytes_for_any_parser_error_path(self):
        cases = {
            "unknown option": [self.BAD_OPTION],
            "unknown option with value": [f"--x\x07=\x9b", "q"],
            "missing value": ["--db"],
            "ambiguous abbreviation": ["--l"],
            "extra positional": ["one", "two\x1b[31m"],
        }
        for label, argv in cases.items():
            with self.subTest(label):
                proc = self.run_raw(*argv)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                stderr = proc.stderr.replace(b"\r\n", b"\n")  # Windows text mode writes CRLF
                bad = [b for b in stderr if b != 0x0A and (b < 0x20 or b == 0x7F)]
                self.assertEqual(bad, [], proc.stderr)
                self.assertTrue(proc.stderr.startswith(b"usage:"), "normal usage line is kept")
                self.assertIn(b"error:", proc.stderr)

    def test_in_process_parser_error_is_escaped_and_the_executor_is_never_called(self):
        spy, out, err = ExecutorSpy(), io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), \
                mock.patch.object(sqlite3, "connect", side_effect=AssertionError("database touched")):
            code = cli.main([self.BAD_OPTION], out=out, execute=spy)
        self.assertEqual((code, spy.calls), (2, []))
        self.assertEqual(raw_controls(err.getvalue()), [])
        self.assertIn("\\x1b[2J", err.getvalue())
        self.assertEqual(out.getvalue(), "")

    def test_help_formatting_is_unchanged(self):
        expected = cli._make_parser().format_help()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["--help"])
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), expected)
        self.assertTrue(expected.startswith("usage: text2sql.cli"))
        self.assertGreater(expected.count("\n"), 8, "multi-line help keeps its newlines")
        for option in ("--db", "--list-questions", "--live", "--model"):
            self.assertIn(option, expected)
        self.assertNotIn("\\n", expected)

    def test_help_through_the_real_entry_point(self):
        proc = self.run_raw("--help")
        self.assertEqual((proc.returncode, proc.stderr), (0, b""))
        self.assertEqual(proc.stdout.decode().replace("\r\n", "\n"), cli._make_parser().format_help())


class CliUsage(CliBase):
    def test_unknown_question_is_exit_2_and_runs_nothing(self):
        spy = ExecutorSpy()
        code, text = self.run_cli("What is the airspeed of a swallow?", execute=spy)
        self.assertEqual((code, spy.calls), (2, []))
        self.assertIn("does not know that question", text)

    def test_missing_question_or_db_is_exit_2(self):
        spy = ExecutorSpy()
        for argv in ([], [Q_JOINED], ["--db", str(self.db_path)]):
            with self.subTest(argv=argv):
                out = io.StringIO()
                self.assertEqual(cli.main(argv, out=out, execute=spy), 2)
        self.assertEqual(spy.calls, [])

    def test_list_questions_lists_the_six_and_is_labelled(self):
        out = io.StringIO()
        self.assertEqual(cli.main(["--list-questions"], out=out), 0)
        text = out.getvalue()
        self.assertTrue(text.startswith(LABEL))
        for q in (Q_JOINED, Q_FEB1, Q_COUNT, Q_MARCH, Q_AMBIGUOUS, Q_FORBIDDEN):
            self.assertIn(q, text)

    def test_unreadable_database_is_a_status_not_a_traceback(self):
        out = io.StringIO()
        missing = self.db_path.with_name("missing.sqlite3")
        code = cli.main([Q_JOINED, "--db", str(missing)], out=out)
        self.assertEqual(code, 1)
        self.assertIn("Status: error", out.getvalue())


if __name__ == "__main__":
    unittest.main()
