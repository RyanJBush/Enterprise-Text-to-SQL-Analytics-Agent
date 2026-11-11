"""Executor tests, aimed at the real restricted SQLite connection.

Two rules for reading these tests:

* ``FixtureTestCase.tearDown`` fails ANY test that changes the fixture bytes or
  leaves a journal file next to it, so "fixture unchanged" is asserted everywhere.
* Authorizer tests call ``RestrictedDb.run`` directly. That skips the input gate on
  purpose: a rejection by ``check_input`` must never stand in for a database denial.
"""

from __future__ import annotations

import itertools
import sqlite3
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import FixtureTestCase
from text2sql import executor
from text2sql.executor import (
    DEADLINE_S,
    MAX_COLUMNS,
    MAX_COMPOUND_TERMS,
    MAX_EXPR_DEPTH,
    MAX_ROWS,
    MAX_SQL_BYTES,
    MAX_VALUE_BYTES,
    MAX_VM_INSTRUCTIONS,
    PROGRESS_INTERVAL,
    STATUS_MESSAGES,
    Authorizer,
    ExecutorConfigError,
    RestrictedDb,
    Status,
    check_input,
    execute_query,
)

# --------------------------------------------------------------------- SQL used below
JOINED = """SELECT p.category, SUM(o.quantity * o.unit_price_cents) AS revenue_cents
FROM orders AS o
JOIN products AS p ON p.product_id = o.product_id
WHERE o.status = 'paid'
  AND o.order_date >= '2025-01-01'
  AND o.order_date < '2025-02-01'
GROUP BY p.category
ORDER BY revenue_cents DESC, p.category ASC;"""

# 8 rows ^ 7 tables = 2,097,152 combinations: far beyond 100,000 VM instructions.
CROSS7 = "SELECT count(*) FROM " + ", ".join(f"orders AS t{i}" for i in range(7))

# First row is emitted immediately (order_id = 101 short-circuits the OR). The expensive
# uncorrelated subquery only runs when the SECOND row is examined, i.e. during fetching.
WORK_DURING_FETCH = (
    "SELECT x.order_id FROM orders AS x WHERE x.order_id = 101 OR (" + CROSS7 + ") > 0"
)


def _concat(n: int) -> str:
    return " || ".join(["s"] * n)


# 'Hardware' (8 bytes) -> 128 -> 2,048 -> 32,768 bytes in d.s, built from allowed syntax only.
_BIG_CTE = (
    "WITH a AS (SELECT category AS s FROM products WHERE product_id = 1), "
    f"b AS (SELECT {_concat(16)} AS s FROM a), "
    f"c AS (SELECT {_concat(16)} AS s FROM b), "
    f"d AS (SELECT {_concat(16)} AS s FROM c) "
)
VALUE_AT_LIMIT = _BIG_CTE + "SELECT s || s AS s FROM d"  # 65,536 bytes
VALUE_OVER_LIMIT = _BIG_CTE + "SELECT s || s || 'x' AS s FROM d"  # 65,537 bytes

PRODUCT_ROWS = [(1, "Keyboard", "Hardware"), (2, "Monitor", "Hardware"),
                (3, "Notebook", "Supplies"), (4, "Pen Set", "Supplies")]


def action(name: str) -> int:
    return getattr(sqlite3, name)


class DbCase(FixtureTestCase):
    def direct(self, sql: str, **kwargs) -> executor.QueryResult:
        """Run on a fresh restricted connection WITHOUT the input gate."""
        with RestrictedDb(self.db_path, **kwargs) as db:
            return db.run(sql)

    def query(self, sql: str) -> executor.QueryResult:
        return execute_query(sql, db_path=self.db_path)


# ================================================================= acceptance cases
class AcceptanceCases(DbCase):
    def test_joined_question(self):
        r = self.query(JOINED)
        self.assertEqual(r.status, Status.OK, r.detail)
        self.assertEqual(r.columns, ("category", "revenue_cents"))
        self.assertEqual(r.rows, [("Hardware", 13000), ("Supplies", 6500)])

    def test_filter_paid_orders_dated_feb_1(self):
        r = self.query(
            "SELECT order_id FROM orders WHERE status = 'paid' "
            "AND order_date >= '2025-02-01' AND order_date < '2025-02-02' ORDER BY order_id"
        )
        self.assertEqual((r.status, r.rows), (Status.OK, [(107,)]))

    def test_aggregate_paid_orders_in_january(self):
        r = self.query(
            "SELECT COUNT(*) FROM orders WHERE status = 'paid' "
            "AND order_date >= '2025-01-01' AND order_date < '2025-02-01'"
        )
        self.assertEqual((r.status, r.rows), (Status.OK, [(5,)]))

    def test_empty_result_is_ok_with_no_rows(self):
        r = self.query(
            "SELECT order_id FROM orders WHERE status = 'paid' "
            "AND order_date >= '2025-03-01' AND order_date < '2025-04-01'"
        )
        self.assertEqual((r.status, r.rows, r.columns), (Status.OK, [], ("order_id",)))

    def test_forbidden_schema_read_is_denied_by_sqlite_not_the_gate(self):
        sql = "SELECT name, sql FROM main.sqlite_schema;"
        self.assertIsNone(check_input(sql), "the gate must let this through")
        r = self.query(sql)
        self.assertEqual(r.status, Status.DENIED)
        self.assertEqual(r.rows, [])
        self.assertEqual(r.denial.action, sqlite3.SQLITE_READ)
        self.assertEqual(r.denial.arg1, "sqlite_master")  # sqlite_schema's real name


# ===================================================================== input gate
class InputGate(DbCase):
    ACCEPTED = [
        "SELECT 1", "select 1", "SeLeCt 1", "  \n\t\f\rSELECT 1", "SELECT(1)", "SELECT*FROM products",
        "WITH x AS (SELECT 1) SELECT * FROM x", "with x as (select 1) select * from x",
        "SELECT 1;", "SELECT 1 -- trailing comment is fine",
    ]
    REJECTED = [
        "", "   ", "\n", "PRAGMA table_info(orders)", "EXPLAIN SELECT 1", "DELETE FROM orders",
        "INSERT INTO products VALUES (9,'a','b')", "VALUES (1)", "(SELECT 1)",
        "-- comment\nSELECT 1", "/* c */ SELECT 1", "SELECT1", "SELECT_1", "SELECT$x", "WITHx AS (SELECT 1) SELECT 1",
        " SELECT 1",  # NBSP is not SQLite whitespace
        "ſelect 1",  # str.upper() would turn U+017F into 'S'; it is not SQL
        "SELECTé 1",  # non-ASCII continues an identifier
        None, 42, b"SELECT 1",
    ]

    def test_accepts_select_and_with(self):
        for sql in self.ACCEPTED:
            with self.subTest(sql=sql):
                self.assertIsNone(check_input(sql))

    def test_rejects_everything_else(self):
        for sql in self.REJECTED:
            with self.subTest(sql=sql):
                self.assertIsNotNone(check_input(sql))

    def test_rejected_input_never_opens_a_connection(self):
        with mock.patch.object(sqlite3, "connect", side_effect=AssertionError("connected")):
            for sql in self.REJECTED:
                with self.subTest(sql=sql):
                    r = self.query(sql)
                    self.assertEqual(r.status, Status.REJECTED)
                    self.assertEqual(r.rows, [])

    def test_gate_passes_with_delete_and_the_database_is_what_stops_it(self):
        sql = "WITH x AS (SELECT 1) DELETE FROM orders"
        self.assertIsNone(check_input(sql))
        r = self.query(sql)
        self.assertEqual(r.status, Status.DENIED)
        self.assertEqual((r.denial.action, r.denial.arg1), (sqlite3.SQLITE_DELETE, "orders"))


# ============================================================== one statement only
class SingleStatement(DbCase):
    def test_second_statement_is_not_executed(self):
        for sql in ("SELECT 1; SELECT 2", "SELECT 1; DROP TABLE orders", "SELECT 1;\n DELETE FROM orders"):
            with self.subTest(sql=sql):
                r = self.query(sql)
                self.assertEqual((r.status, r.rows), (Status.INVALID_SQL, []))
        self.assertEqual(self.query("SELECT count(*) FROM orders").rows, [(8,)])

    def test_trailing_semicolon_and_whitespace_are_fine(self):
        self.assertEqual(self.query("SELECT 1;  \n").rows, [(1,)])

    def test_semicolon_inside_a_literal_is_not_a_boundary(self):
        r = self.query("SELECT 'a; DROP TABLE orders; --' AS s")
        self.assertEqual((r.status, r.rows), (Status.OK, [("a; DROP TABLE orders; --",)]))

    def test_null_byte_cannot_smuggle_a_second_statement(self):
        r = self.query("SELECT 1\x00; DROP TABLE orders")
        self.assertEqual(r.status, Status.INVALID_SQL)

    def test_syntax_error_and_unknown_names_are_invalid_sql(self):
        for sql in ("SELECT FROM orders", "SELECT nope FROM orders", "SELECT * FROM nope", "SELECT"):
            with self.subTest(sql=sql):
                self.assertEqual(self.query(sql).status, Status.INVALID_SQL)


# ========================================================== the authorizer, in policy
class AuthorizerPolicy(unittest.TestCase):
    """The allow rules, called exactly as SQLite calls them."""

    allows = staticmethod(Authorizer.allows)
    READ, FUNC, SEL = sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_SELECT

    def test_select_is_allowed(self):
        self.assertTrue(self.allows(self.SEL, None, None, None))

    def test_approved_columns_of_main_tables_are_readable(self):
        from text2sql.schema import APPROVED_COLUMNS
        for table, columns in APPROVED_COLUMNS.items():
            for column in columns:
                with self.subTest(table=table, column=column):
                    self.assertTrue(self.allows(self.READ, table, column, "main"))

    def test_private_and_unknown_things_are_not_readable(self):
        cases = [
            ("orders", "internal_note", "main"),
            ("orders", "INTERNAL_NOTE", "main"),
            ("orders", "rowid", "main"),
            ("sqlite_master", "name", "main"),
            ("sqlite_temp_master", "name", "temp"),
            ("pragma_table_info", "cid", "main"),
            ("orders", "order_id", "temp"),  # approved column, wrong database
            ("orders", "order_id", None),  # approved column needs an explicit main
            (None, "order_id", "main"),
        ]
        for table, column, db in cases:
            with self.subTest(table=table, column=column, db=db):
                self.assertFalse(self.allows(self.READ, table, column, db))

    def test_empty_column_read_is_allowed_for_main_or_none_only(self):
        for db in ("main", None):
            with self.subTest(db=db):
                self.assertTrue(self.allows(self.READ, "orders", "", db))
                self.assertTrue(self.allows(self.READ, "products", "", db))
        for db in ("temp", "other"):
            with self.subTest(db=db):
                self.assertFalse(self.allows(self.READ, "orders", "", db))
        self.assertFalse(self.allows(self.READ, "sqlite_master", "", "main"))
        self.assertFalse(self.allows(self.READ, "nope", "", None))

    def test_exactly_six_functions_case_insensitively(self):
        for name in ("count", "COUNT", "Sum", "min", "MAX", "avg", "CoAlEsCe"):
            with self.subTest(name=name):
                self.assertTrue(self.allows(self.FUNC, None, name, None))
        for name in ("upper", "length", "abs", "like", "random", "randomblob", "load_extension",
                     "date", "round", "total", "group_concat", "", None):
            with self.subTest(name=name):
                self.assertFalse(self.allows(self.FUNC, None, name, None))

    def test_every_other_action_is_denied(self):
        for code, name in executor._ACTION_NAMES.items():
            if code in (self.SEL, self.READ, self.FUNC):
                continue
            with self.subTest(action=name):
                self.assertFalse(self.allows(code, "orders", "order_id", "main"))
        self.assertFalse(self.allows(9999, None, None, None))

    def test_callback_returns_deny_and_remembers_only_the_first_denial(self):
        auth = Authorizer()
        self.assertEqual(auth(self.SEL, None, None, None, None), sqlite3.SQLITE_OK)
        self.assertIsNone(auth.denial)
        self.assertEqual(auth(self.READ, "orders", "internal_note", "main", None), sqlite3.SQLITE_DENY)
        self.assertEqual(auth(self.FUNC, None, "upper", None, None), sqlite3.SQLITE_DENY)
        self.assertEqual(auth.denial.arg2, "internal_note")
        self.assertEqual(auth.denial.action_name, "SQLITE_READ")
        auth.reset()
        self.assertIsNone(auth.denial)

    def test_a_bug_in_the_policy_denies_instead_of_raising(self):
        auth = Authorizer()
        self.assertEqual(auth(self.READ, 5, "x", "main", None), sqlite3.SQLITE_DENY)  # int has no .lower()


# ======================================================= the authorizer, on a real DB
class AuthorizerOnRealConnection(DbCase):
    # (label, sql, SQLITE_ action, arg1 or None, arg2 or None) -- arg values are checked case-insensitively.
    # CREATE/DROP/ANALYZE are first denied as writes to sqlite_master; that is what SQLite asks first.
    DENIED = [
        ("schema read", "SELECT name, sql FROM main.sqlite_schema", "SQLITE_READ", "sqlite_master", "name"),
        ("schema read via alias", "SELECT name FROM sqlite_master", "SQLITE_READ", "sqlite_master", "name"),
        ("temp schema", "SELECT * FROM temp.sqlite_temp_master", "SQLITE_READ", "sqlite_temp_master", None),
        ("private column", "SELECT internal_note FROM orders", "SQLITE_READ", "orders", "internal_note"),
        ("star hits private column", "SELECT * FROM orders", "SQLITE_READ", "orders", "internal_note"),
        ("alias star hits private column", "SELECT o.* FROM orders AS o", "SQLITE_READ", "orders", "internal_note"),
        ("order by private column", "SELECT order_id FROM orders ORDER BY internal_note", "SQLITE_READ", "orders", "internal_note"),
        ("where private column", "SELECT order_id FROM orders WHERE internal_note = 'x'", "SQLITE_READ", "orders", "internal_note"),
        ("aggregate of private column", "SELECT count(internal_note) FROM orders", "SQLITE_READ", "orders", "internal_note"),
        ("private column in a join", "SELECT p.category FROM products AS p JOIN orders AS o ON o.product_id = p.product_id WHERE o.internal_note IS NOT NULL", "SQLITE_READ", "orders", "internal_note"),
        ("private column in a scalar subquery", "SELECT (SELECT internal_note FROM orders LIMIT 1)", "SQLITE_READ", "orders", "internal_note"),
        ("private column inside a CTE", "WITH x AS (SELECT internal_note FROM orders) SELECT * FROM x", "SQLITE_READ", "orders", "internal_note"),
        ("pragma_table_info", "SELECT * FROM pragma_table_info('orders')", "SQLITE_READ", "pragma_table_info", None),
        ("load_extension", "SELECT load_extension('x')", "SQLITE_FUNCTION", None, "load_extension"),
        ("randomblob", "SELECT randomblob(8)", "SQLITE_FUNCTION", None, "randomblob"),
        ("random", "SELECT random()", "SQLITE_FUNCTION", None, "random"),
        ("upper", "SELECT upper(category) FROM products", "SQLITE_FUNCTION", None, "upper"),
        ("LIKE is a function", "SELECT category LIKE 'H%' FROM products", "SQLITE_FUNCTION", None, "like"),
        ("date", "SELECT date('now')", "SQLITE_FUNCTION", None, "date"),
        ("sqlite_version", "SELECT sqlite_version()", "SQLITE_FUNCTION", None, "sqlite_version"),
        ("WITH + DELETE", "WITH x AS (SELECT 1) DELETE FROM orders", "SQLITE_DELETE", "orders", None),
        ("WITH + UPDATE", "WITH x AS (SELECT 1) UPDATE orders SET quantity = 1", "SQLITE_UPDATE", "orders", "quantity"),
        ("WITH + INSERT", "WITH x AS (SELECT 1) INSERT INTO products VALUES (9, 'a', 'b')", "SQLITE_INSERT", "products", None),
        ("INSERT", "INSERT INTO products VALUES (9, 'a', 'b')", "SQLITE_INSERT", "products", None),
        ("UPDATE", "UPDATE orders SET quantity = 1", "SQLITE_UPDATE", "orders", "quantity"),
        ("DELETE", "DELETE FROM orders", "SQLITE_DELETE", "orders", None),
        ("CREATE TABLE", "CREATE TABLE t (x)", "SQLITE_INSERT", "sqlite_master", None),
        ("CREATE TEMP TABLE", "CREATE TEMP TABLE t (x)", "SQLITE_INSERT", "sqlite_temp_master", None),
        ("CREATE INDEX", "CREATE INDEX i ON orders (status)", "SQLITE_INSERT", "sqlite_master", None),
        ("CREATE VIEW", "CREATE VIEW v AS SELECT 1", "SQLITE_INSERT", "sqlite_master", None),
        ("DROP TABLE", "DROP TABLE orders", "SQLITE_DELETE", "sqlite_master", None),
        ("ALTER TABLE", "ALTER TABLE orders ADD COLUMN z", "SQLITE_ALTER_TABLE", None, "orders"),
        ("ANALYZE", "ANALYZE", "SQLITE_INSERT", "sqlite_master", None),
        ("REINDEX", "REINDEX", "SQLITE_REINDEX", None, None),
        ("VACUUM", "VACUUM", "SQLITE_ATTACH", None, None),
        ("BEGIN", "BEGIN", "SQLITE_TRANSACTION", "BEGIN", None),
        ("COMMIT", "COMMIT", "SQLITE_TRANSACTION", "COMMIT", None),
        ("SAVEPOINT", "SAVEPOINT s", "SQLITE_SAVEPOINT", None, "s"),
        ("ATTACH", "ATTACH DATABASE ':memory:' AS scratch", "SQLITE_ATTACH", ":memory:", None),
        ("DETACH", "DETACH DATABASE scratch", "SQLITE_DETACH", "scratch", None),
        ("PRAGMA read", "PRAGMA table_info(orders)", "SQLITE_PRAGMA", "table_info", "orders"),
        ("PRAGMA write", "PRAGMA query_only = OFF", "SQLITE_PRAGMA", "query_only", "OFF"),
        ("recursive CTE", "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c WHERE n < 10) SELECT n FROM c", "SQLITE_RECURSIVE", None, None),
    ]

    def test_each_forbidden_statement_is_denied_with_the_expected_action(self):
        for label, sql, name, arg1, arg2 in self.DENIED:
            with self.subTest(label):
                r = self.direct(sql)  # no input gate in this path
                self.assertEqual(r.status, Status.DENIED, r.detail)
                self.assertEqual(r.rows, [])
                self.assertIsNotNone(r.denial, "DENIED must carry the authorizer's denial")
                self.assertEqual(r.denial.action, action(name), r.denial)
                if arg1 is not None:
                    self.assertEqual((r.denial.arg1 or "").lower(), arg1.lower(), r.denial)
                if arg2 is not None:
                    self.assertEqual((r.denial.arg2 or "").lower(), arg2.lower(), r.denial)

    ALLOWED = [
        ("explicit approved columns", "SELECT product_id, product_name, category FROM products ORDER BY product_id", PRODUCT_ROWS),
        ("star on a table without private columns", "SELECT * FROM products ORDER BY product_id", PRODUCT_ROWS),
        ("unqualified COUNT(*) (dbname None)", "SELECT COUNT(*) FROM orders", [(8,)]),
        ("main-qualified count(*)", "SELECT count(*) FROM main.orders", [(8,)]),
        ("all six functions, mixed case", "SELECT COUNT(*), Sum(quantity), MIN(order_date), max(order_date), AVG(quantity), CoAlEsCe(NULL, 7) FROM orders", [(8, 21, "2024-12-31", "2025-02-01", 2.625, 7)]),
        ("two-argument min/max", "SELECT min(3, 2), max(3, 2)", [(2, 3)]),
        ("nonrecursive CTE", "WITH paid AS (SELECT order_id FROM orders WHERE status = 'paid') SELECT count(*) FROM paid", [(7,)]),
        ("compound select", "SELECT 1 UNION ALL SELECT 2", [(1,), (2,)]),
        ("join over every approved column", "SELECT o.order_id, o.order_date, o.product_id, o.quantity, o.unit_price_cents, o.status, p.product_name, p.category FROM orders AS o JOIN products AS p ON p.product_id = o.product_id WHERE o.order_id = 101", [(101, "2025-01-01", 1, 2, 2500, "paid", "Keyboard", "Hardware")]),
    ]

    def test_each_legitimate_statement_is_allowed(self):
        for label, sql, rows in self.ALLOWED:
            with self.subTest(label):
                r = self.direct(sql)
                self.assertEqual(r.status, Status.OK, f"{r.detail} {r.denial}")
                self.assertEqual(r.rows, rows)
                self.assertIsNone(r.denial)

    def test_unqualified_count_star_really_reaches_the_authorizer_with_dbname_none(self):
        # Pins the premise behind the `dbname is None` allowance in the spec.
        with RestrictedDb(self.db_path) as db:
            seen = []
            db.conn.set_authorizer(lambda *a: (seen.append(a), sqlite3.SQLITE_OK)[1])
            db.conn.execute("SELECT COUNT(*) FROM orders").fetchall()
        self.assertIn((sqlite3.SQLITE_READ, "orders", "", None, None), seen)

    def test_denial_is_forgotten_between_runs_on_one_connection(self):
        with RestrictedDb(self.db_path) as db:
            self.assertEqual(db.run("SELECT internal_note FROM orders").status, Status.DENIED)
            ok = db.run("SELECT count(*) FROM orders")
            self.assertEqual((ok.status, ok.rows, ok.denial), (Status.OK, [(8,)], None))


# ================================================= lower layers, authorizer removed
class LowerLayers(DbCase):
    """Remove the authorizer and prove the file mode and pragmas still hold the line."""

    def test_file_mode_blocks_writes_without_authorizer_and_without_query_only(self):
        with RestrictedDb(self.db_path) as db:
            db.conn.set_authorizer(None)
            db.conn.execute("PRAGMA query_only=OFF")  # drop the pragma layer too
            for sql in ("INSERT INTO products VALUES (9, 'a', 'b')", "DELETE FROM orders",
                        "CREATE TABLE t (x)", "UPDATE orders SET quantity = 99"):
                with self.subTest(sql=sql), self.assertRaises(sqlite3.OperationalError) as ctx:
                    db.conn.execute(sql)
                self.assertEqual(ctx.exception.sqlite_errorcode, sqlite3.SQLITE_READONLY)

    def test_trusted_setup_values_are_applied(self):
        with RestrictedDb(self.db_path) as db:
            db.conn.set_authorizer(None)  # PRAGMA reads are denied while it is installed
            read = lambda name: db.conn.execute(f"PRAGMA {name}").fetchone()[0]
            self.assertEqual(read("query_only"), 1)
            self.assertEqual(read("trusted_schema"), 0)
            self.assertEqual(read("temp_store"), 2)  # MEMORY
            self.assertEqual(read("busy_timeout"), 50)

    def test_sqlite_limits_are_applied(self):
        with RestrictedDb(self.db_path) as db:
            got = {
                "sql": db.conn.getlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH),
                "value": db.conn.getlimit(sqlite3.SQLITE_LIMIT_LENGTH),
                "columns": db.conn.getlimit(sqlite3.SQLITE_LIMIT_COLUMN),
                "depth": db.conn.getlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH),
                "compound": db.conn.getlimit(sqlite3.SQLITE_LIMIT_COMPOUND_SELECT),
            }
        self.assertEqual(got, {"sql": 8 * 1024, "value": 64 * 1024, "columns": 12, "depth": 50, "compound": 8})

    def test_extension_loading_stays_disabled(self):
        with RestrictedDb(self.db_path) as db:
            db.conn.set_authorizer(None)
            with self.assertRaises(sqlite3.OperationalError) as ctx:
                db.conn.execute("SELECT load_extension('nonexistent')")
        self.assertIn("not authorized", str(ctx.exception))

    def test_connection_uses_the_read_only_uri_and_a_fresh_cache(self):
        spy = ConnectSpy()
        with mock.patch.object(sqlite3, "connect", spy):
            self.query("SELECT 1")
        (args, kwargs), = spy.calls
        self.assertEqual(args[0], f"{self.db_path.as_uri()}?mode=ro")
        self.assertTrue(args[0].startswith("file:///"), "absolute file URI")
        self.assertIs(kwargs["uri"], True)
        self.assertEqual(kwargs["cached_statements"], 0)
        self.assertEqual(kwargs["timeout"], 0.05)

    def test_relative_paths_are_a_programming_error(self):
        with self.assertRaises(ValueError):
            RestrictedDb("fixture.sqlite3")
        with self.assertRaises(ValueError):
            execute_query("SELECT 1", db_path="fixture.sqlite3")


# ================================================================ configured limits
class SizeLimits(DbCase):
    def test_sql_text_limit_is_8_kib_of_utf8_bytes(self):
        at_limit = "SELECT 1" + " " * (MAX_SQL_BYTES - len("SELECT 1"))
        self.assertEqual(len(at_limit.encode()), MAX_SQL_BYTES)
        self.assertEqual(self.query(at_limit).status, Status.OK)
        over = self.query(at_limit + " ")
        self.assertEqual(over.status, Status.LIMIT_SIZE, over.detail)

    def test_sql_text_limit_counts_bytes_not_characters(self):
        sql = "SELECT 1 /*" + "é" * 4600 + "*/"  # 4,613 characters, 9,213 bytes
        self.assertLess(len(sql), MAX_SQL_BYTES)
        self.assertGreater(len(sql.encode()), MAX_SQL_BYTES)
        self.assertEqual(self.query(sql).status, Status.LIMIT_SIZE)

    def test_value_limit_is_64_kib(self):
        ok = self.query(VALUE_AT_LIMIT)
        self.assertEqual(ok.status, Status.OK, ok.detail)
        self.assertEqual(len(ok.rows[0][0].encode()), MAX_VALUE_BYTES)
        over = self.query(VALUE_OVER_LIMIT)
        self.assertEqual((over.status, over.rows), (Status.LIMIT_SIZE, []), over.detail)
        self.assertIn("too big", over.detail)

    def test_result_column_limit_is_12(self):
        twelve = "SELECT " + ", ".join(str(i) for i in range(MAX_COLUMNS))
        thirteen = "SELECT " + ", ".join(str(i) for i in range(MAX_COLUMNS + 1))
        self.assertEqual(self.query(twelve).status, Status.OK)
        r = self.query(thirteen)
        self.assertEqual(r.status, Status.LIMIT_SIZE, r.detail)

    def test_expression_depth_limit_is_50(self):
        chain = lambda n: "SELECT " + " + ".join(["1"] * n)
        ok = self.query(chain(MAX_EXPR_DEPTH))
        self.assertEqual((ok.status, ok.rows), (Status.OK, [(50,)]), ok.detail)
        over = self.query(chain(MAX_EXPR_DEPTH + 1))
        self.assertEqual(over.status, Status.LIMIT_SIZE, over.detail)

    def test_compound_term_limit_is_8(self):
        union = lambda n: " UNION ALL ".join(["SELECT 1"] * n)
        self.assertEqual(self.query(union(MAX_COMPOUND_TERMS)).status, Status.OK)
        over = self.query(union(MAX_COMPOUND_TERMS + 1))
        self.assertEqual(over.status, Status.LIMIT_SIZE, over.detail)


class RowLimit(DbCase):
    ROWS = "SELECT a.order_id FROM orders AS a, orders AS b, orders AS c LIMIT {}"  # 512 combinations

    def test_100_rows_are_returned(self):
        r = self.query(self.ROWS.format(MAX_ROWS))
        self.assertEqual((r.status, len(r.rows)), (Status.OK, 100))

    def test_101_rows_is_a_row_limit_with_no_partial_answer(self):
        r = self.query(self.ROWS.format(MAX_ROWS + 1))
        self.assertEqual((r.status, r.rows), (Status.LIMIT_ROWS, []))
        self.assertEqual(r.columns, ("order_id",))
        self.assertIn(str(MAX_ROWS), r.message)

    def test_unbounded_result_is_a_row_limit(self):
        r = self.query("SELECT a.order_id FROM orders AS a, orders AS b, orders AS c")
        self.assertEqual((r.status, r.rows), (Status.LIMIT_ROWS, []))

    def test_fewer_than_100_rows_and_zero_rows_are_ok(self):
        self.assertEqual(len(self.query("SELECT order_id FROM orders").rows), 8)
        self.assertEqual(self.query("SELECT order_id FROM orders WHERE 0").rows, [])


class WorkBudget(DbCase):
    def test_instruction_budget_interrupts_a_runaway_join(self):
        with RestrictedDb(self.db_path, clock=lambda: 0.0) as db:  # frozen clock: only instructions count
            r = db.run(CROSS7)
            self.assertEqual((r.status, r.rows), (Status.LIMIT_WORK, []), r.detail)
            self.assertEqual(db.tripped, "vm_instructions")
            self.assertEqual(db.progress_ticks, MAX_VM_INSTRUCTIONS // PROGRESS_INTERVAL)

    def test_deadline_interrupts_on_the_first_callback_that_sees_it(self):
        times = itertools.chain([0.0], itertools.repeat(DEADLINE_S + 1))  # run start, then "late" forever
        with RestrictedDb(self.db_path, clock=lambda: next(times)) as db:
            r = db.run(CROSS7)
            self.assertEqual(r.status, Status.LIMIT_WORK, r.detail)
            self.assertEqual(db.tripped, "deadline")
            self.assertEqual(db.progress_ticks, 1)  # first callback, 1,000 instructions in

    def test_default_clock_is_monotonic(self):
        with RestrictedDb(self.db_path) as db:
            self.assertIs(db._clock, time.monotonic)

    def test_progress_handler_covers_fetching_not_just_execute(self):
        # Premise: with a plain counting handler, execute() does no heavy work but the fetch does.
        with RestrictedDb(self.db_path) as db:
            ticks = []
            db.conn.set_progress_handler(lambda: ticks.append(1) and 0, PROGRESS_INTERVAL)
            cur = db.conn.execute(WORK_DURING_FETCH)
            self.assertEqual(len(ticks), 0, "execute() should only produce the first row")
            cur.fetchmany(MAX_ROWS + 1)
            self.assertGreater(len(ticks), MAX_VM_INSTRUCTIONS // PROGRESS_INTERVAL)
            cur.close()
        # The real thing: the executor's handler must stop that same work.
        r = self.direct(WORK_DURING_FETCH, clock=lambda: 0.0)
        self.assertEqual((r.status, r.rows), (Status.LIMIT_WORK, []), r.detail)

    def test_legitimate_queries_use_a_small_fraction_of_the_budget(self):
        with RestrictedDb(self.db_path) as db:
            self.assertEqual(db.run(JOINED).status, Status.OK)
            self.assertLess(db.progress_ticks, 10)

    def test_same_connection_reads_again_after_an_interrupt(self):
        with RestrictedDb(self.db_path) as db:
            self.assertEqual(db.run(CROSS7).status, Status.LIMIT_WORK)
            again = db.run("SELECT count(*) FROM orders")
            self.assertEqual((again.status, again.rows), (Status.OK, [(8,)]))
            self.assertIsNone(db.tripped)

    def test_fresh_connection_reads_after_an_interrupt(self):
        self.assertEqual(self.query(CROSS7).status, Status.LIMIT_WORK)
        r = self.query(JOINED)
        self.assertEqual((r.status, r.rows), (Status.OK, [("Hardware", 13000), ("Supplies", 6500)]))


# ============================================================ statuses and lifecycle
class ConnectSpy:
    """Stands in for sqlite3.connect and remembers every connection it handed out."""

    def __init__(self) -> None:
        self._real = sqlite3.connect
        self.connections: list[sqlite3.Connection] = []
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        con = self._real(*args, **kwargs)
        self.connections.append(con)
        return con


def assert_closed(case: unittest.TestCase, con: sqlite3.Connection) -> None:
    with case.assertRaises(sqlite3.ProgrammingError):
        con.execute("SELECT 1")


class StatusesAndLifecycle(DbCase):
    SAMPLES = {
        Status.OK: "SELECT 1",
        Status.DENIED: "SELECT internal_note FROM orders",
        Status.INVALID_SQL: "SELECT nope FROM orders",
        Status.LIMIT_WORK: CROSS7,
        Status.LIMIT_SIZE: "SELECT " + ", ".join(str(i) for i in range(MAX_COLUMNS + 1)),
        Status.LIMIT_ROWS: RowLimit.ROWS.format(MAX_ROWS + 1),
    }

    def test_each_failure_class_has_its_own_status_and_message(self):
        for status, sql in self.SAMPLES.items():
            with self.subTest(status=status.value):
                self.assertEqual(self.query(sql).status, status)
        self.assertEqual(self.query("PRAGMA x").status, Status.REJECTED)
        self.assertEqual(set(self.SAMPLES) | {Status.REJECTED, Status.ERROR}, set(Status))
        self.assertEqual(set(STATUS_MESSAGES), set(Status))
        self.assertEqual(len(set(STATUS_MESSAGES.values())), len(Status), "messages must be distinct")

    def test_connection_is_closed_on_every_path(self):
        for status, sql in self.SAMPLES.items():
            with self.subTest(status=status.value):
                spy = ConnectSpy()
                with mock.patch.object(sqlite3, "connect", spy):
                    self.assertEqual(self.query(sql).status, status)
                self.assertEqual(len(spy.connections), 1)
                assert_closed(self, spy.connections[0])

    def test_every_request_gets_a_fresh_connection(self):
        spy = ConnectSpy()
        with mock.patch.object(sqlite3, "connect", spy):
            self.query("SELECT 1")
            self.query("SELECT 1")
        self.assertEqual(len(spy.connections), 2)
        self.assertIsNot(spy.connections[0], spy.connections[1])

    def test_unreadable_database_is_an_error_status_not_a_crash(self):
        spy = ConnectSpy()
        with mock.patch.object(sqlite3, "connect", spy):
            r = execute_query("SELECT 1", db_path=self.db_path.with_name("missing.sqlite3"))
        self.assertEqual(r.status, Status.ERROR)
        self.assertIn("unable to open", r.detail)
        self.assertEqual(spy.connections, [])

    def test_setup_failure_closes_the_connection_and_fails_closed(self):
        spy = ConnectSpy()
        boom = ExecutorConfigError("pragma not applied")
        with mock.patch.object(sqlite3, "connect", spy), \
                mock.patch.object(RestrictedDb, "_configure", side_effect=boom):
            r = self.query("SELECT 1")
        self.assertEqual(r.status, Status.ERROR)
        self.assertIn("pragma not applied", r.detail)
        self.assertEqual(len(spy.connections), 1)
        assert_closed(self, spy.connections[0])

    def test_unexpected_exception_still_closes_the_connection(self):
        spy = ConnectSpy()
        with mock.patch.object(sqlite3, "connect", spy), \
                mock.patch.object(RestrictedDb, "run", side_effect=RuntimeError("bug")):
            with self.assertRaises(RuntimeError):
                self.query("SELECT 1")
        assert_closed(self, spy.connections[0])


if __name__ == "__main__":
    unittest.main()
