"""Fixture tests: the seeded database matches the handoff exactly."""

from __future__ import annotations

import re
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import FixtureTestCase
from text2sql import seed as seed_module
from text2sql.schema import APPROVED_COLUMNS
from text2sql.seed import SEED_SQL_PATH, SeedError, seed_database

PRODUCTS = [
    (1, "Keyboard", "Hardware"),
    (2, "Monitor", "Hardware"),
    (3, "Notebook", "Supplies"),
    (4, "Pen Set", "Supplies"),
]
ORDERS = [
    (101, "2025-01-01", 1, 2, 2500, "paid"),
    (102, "2025-01-10", 2, 1, 6000, "paid"),
    (103, "2025-01-15", 3, 3, 1500, "paid"),
    (104, "2025-01-20", 1, 1, 2000, "paid"),
    (105, "2025-01-31", 4, 2, 1000, "paid"),
    (106, "2025-01-31", 2, 10, 6000, "cancelled"),
    (107, "2025-02-01", 2, 1, 5500, "paid"),
    (108, "2024-12-31", 3, 1, 1500, "paid"),
]
VALID_PRODUCT = (9, "Mouse", "Hardware")
VALID_ORDER = (200, "2025-03-01", 1, 1, 100, "paid", "note")


class SeededContent(FixtureTestCase):
    def ro(self) -> sqlite3.Connection:
        con = sqlite3.connect(f"{self.db_path.as_uri()}?mode=ro", uri=True)
        self.addCleanup(con.close)
        return con

    def test_rows_match_the_handoff_exactly(self):
        con = self.ro()
        self.assertEqual(
            con.execute("SELECT * FROM products ORDER BY product_id").fetchall(), PRODUCTS
        )
        self.assertEqual(
            con.execute(
                "SELECT order_id, order_date, product_id, quantity, unit_price_cents, status "
                "FROM orders ORDER BY order_id"
            ).fetchall(),
            ORDERS,
        )

    def test_internal_note_defaults_to_fixture_only(self):
        notes = self.ro().execute("SELECT DISTINCT internal_note FROM orders").fetchall()
        self.assertEqual(notes, [("fixture-only",)])

    def test_order_dates_are_iso_calendar_dates(self):
        for (date,) in self.ro().execute("SELECT order_date FROM orders"):
            self.assertRegex(date, r"^\d{4}-\d{2}-\d{2}$")

    def test_only_the_two_seed_tables_exist(self):
        # No views, triggers or indexes: the authorizer's `dbname is None` case relies on it.
        self.assertEqual(
            self.ro().execute("SELECT type, name FROM sqlite_schema ORDER BY name").fetchall(),
            [("table", "orders"), ("table", "products")],
        )

    def test_both_tables_are_without_rowid(self):
        wr = dict(
            self.ro().execute(
                "SELECT name, wr FROM pragma_table_list WHERE name IN ('orders','products')"
            )
        )
        self.assertEqual(wr, {"orders": 1, "products": 1})

    def test_foreign_key_check_is_clean(self):
        self.assertEqual(self.ro().execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_revenue_definition_matches_acceptance_numbers(self):
        # quantity * unit_price_cents on paid orders, integer cents.
        total = self.ro().execute(
            "SELECT SUM(quantity * unit_price_cents) FROM orders WHERE status = 'paid' "
            "AND order_date >= '2025-01-01' AND order_date < '2025-02-01'"
        ).fetchone()[0]
        self.assertEqual(total, 13000 + 6500)

    def test_approved_schema_types_match_the_real_declared_types(self):
        from text2sql.schema import APPROVED_SCHEMA
        con = self.ro()
        for table, columns in APPROVED_SCHEMA.items():
            declared = {r[1]: r[2] for r in con.execute(f"PRAGMA table_info({table})")}
            for name, type_ in columns:
                self.assertEqual(declared[name], type_, f"{table}.{name}")

    def test_approved_columns_are_all_real_and_only_internal_note_is_hidden(self):
        con = self.ro()
        for table, approved in APPROVED_COLUMNS.items():
            actual = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
            self.assertTrue(set(approved) <= set(actual), table)
            hidden = set(actual) - set(approved)
            self.assertEqual(hidden, {"internal_note"} if table == "orders" else set(), table)


class SeededConstraints(FixtureTestCase):
    """Constraints are tested on a writable COPY so the fixture itself stays untouched."""

    def writable(self) -> sqlite3.Connection:
        # Own directory: the fixture's directory listing is part of the integrity check.
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        copy = Path(scratch.name) / "copy.sqlite3"
        shutil.copyfile(self.db_path, copy)
        con = sqlite3.connect(copy, isolation_level=None)
        self.addCleanup(con.close)  # runs before scratch.cleanup (LIFO)
        con.execute("PRAGMA foreign_keys=ON")
        return con

    def test_valid_rows_insert(self):
        con = self.writable()
        con.execute("INSERT INTO products VALUES (?,?,?)", VALID_PRODUCT)
        con.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?)", VALID_ORDER)

    def test_every_column_is_required(self):
        con = self.writable()
        for i, name in enumerate(("product_id", "product_name", "category")):
            row = list(VALID_PRODUCT)
            row[i] = None
            with self.subTest(table="products", column=name), self.assertRaises(sqlite3.IntegrityError):
                con.execute("INSERT INTO products VALUES (?,?,?)", row)
        names = (
            "order_id", "order_date", "product_id", "quantity",
            "unit_price_cents", "status", "internal_note",
        )
        for i, name in enumerate(names):
            row = list(VALID_ORDER)
            row[i] = None
            with self.subTest(table="orders", column=name), self.assertRaises(sqlite3.IntegrityError):
                con.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?)", row)

    def test_check_constraints(self):
        con = self.writable()
        bad = {
            "quantity = 0": {3: 0},
            "quantity < 0": {3: -1},
            "negative price": {4: -1},
            "status refunded": {5: "refunded"},
            "status case-sensitive": {5: "PAID"},
        }
        for label, overrides in bad.items():
            row = list(VALID_ORDER)
            for index, value in overrides.items():
                row[index] = value
            with self.subTest(label), self.assertRaises(sqlite3.IntegrityError):
                con.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?)", row)

    def test_zero_price_is_allowed(self):
        row = list(VALID_ORDER)
        row[4] = 0
        self.writable().execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?)", row)

    def test_foreign_key_is_enforced(self):
        row = list(VALID_ORDER)
        row[2] = 99
        with self.assertRaises(sqlite3.IntegrityError):
            self.writable().execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?)", row)

    def test_internal_note_default_applies_when_omitted(self):
        con = self.writable()
        con.execute(
            "INSERT INTO orders (order_id, order_date, product_id, quantity, unit_price_cents, "
            "status) VALUES (201, '2025-03-02', 1, 1, 1, 'paid')"
        )
        self.assertEqual(
            con.execute("SELECT internal_note FROM orders WHERE order_id = 201").fetchone(),
            ("fixture-only",),
        )


class SeedProcedure(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.target = Path(self._tmp.name) / "fresh.sqlite3"

    def test_checked_in_sql_is_one_transaction(self):
        text = SEED_SQL_PATH.read_text(encoding="utf-8")
        code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("--"))
        statements = [s.strip() for s in code.split(";") if s.strip()]
        self.assertEqual(statements[0].upper(), "BEGIN")
        self.assertEqual(statements[-1].upper(), "COMMIT")
        self.assertEqual(sum(s.upper() in ("BEGIN", "COMMIT") for s in statements), 2)
        self.assertIsNone(re.search(r"(?im)^\s*pragma\b", code), "pragmas belong in seed.py")

    def test_refuses_to_overwrite_unless_asked(self):
        seed_database(self.target)
        with self.assertRaises(FileExistsError):
            seed_database(self.target)
        seed_database(self.target, replace=True)  # rebuilds cleanly
        con = sqlite3.connect(f"{self.target.as_uri()}?mode=ro", uri=True)
        try:
            self.assertEqual(con.execute("SELECT count(*) FROM orders").fetchone(), (8,))
        finally:
            con.close()

    def test_relative_path_is_rejected(self):
        with self.assertRaises(ValueError):
            seed_database("relative.sqlite3")

    def test_failing_script_leaves_nothing_behind(self):
        # Valid DDL, then a CHECK violation: the whole transaction must vanish.
        script = (
            "BEGIN;"
            "CREATE TABLE t (n INTEGER CHECK (n > 0));"
            "INSERT INTO t VALUES (1);"
            "INSERT INTO t VALUES (-1);"
            "COMMIT;"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            seed_database(self.target, script=script)
        self.assertEqual(list(Path(self._tmp.name).iterdir()), [])

    def test_foreign_keys_are_enforced_while_seeding(self):
        # Proves PRAGMA foreign_keys=ON ran before the script's BEGIN.
        script = (
            "BEGIN;"
            "CREATE TABLE p (id INTEGER PRIMARY KEY);"
            "CREATE TABLE c (pid INTEGER REFERENCES p(id));"
            "INSERT INTO c VALUES (99);"
            "COMMIT;"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            seed_database(self.target, script=script)
        self.assertEqual(list(Path(self._tmp.name).iterdir()), [])

    # ---- regression: publishing must never destroy files it did not create ----
    BAD_SCRIPT = "BEGIN; CREATE TABLE t (n INTEGER CHECK (n > 0)); INSERT INTO t VALUES (-1); COMMIT;"

    def names(self) -> list[str]:
        return sorted(p.name for p in Path(self._tmp.name).iterdir())

    def test_preexisting_dot_tmp_sibling_survives_a_successful_seed(self):
        sibling = self.target.with_name(self.target.name + ".tmp")
        sibling.write_bytes(b"someone else's data")
        seed_database(self.target)
        self.assertEqual(sibling.read_bytes(), b"someone else's data")
        self.assertEqual(self.names(), sorted([self.target.name, sibling.name]))

    def test_preexisting_dot_tmp_sibling_survives_a_failed_seed(self):
        sibling = self.target.with_name(self.target.name + ".tmp")
        sibling.write_bytes(b"someone else's data")
        with self.assertRaises(sqlite3.IntegrityError):
            seed_database(self.target, script=self.BAD_SCRIPT)
        self.assertEqual(sibling.read_bytes(), b"someone else's data")
        self.assertEqual(self.names(), [sibling.name])  # only this run's own temp file was removed

    def test_destination_created_during_seeding_is_not_overwritten(self):
        real_connect = sqlite3.connect

        def racing_connect(*args, **kwargs):
            # Runs after seed_database's early existence check, before it publishes.
            self.target.write_bytes(b"created by another process")
            return real_connect(*args, **kwargs)

        with mock.patch.object(seed_module.sqlite3, "connect", racing_connect):
            with self.assertRaises(FileExistsError):
                seed_database(self.target)
        self.assertEqual(self.target.read_bytes(), b"created by another process")
        self.assertEqual(self.names(), [self.target.name], "our temp file must be cleaned up")

    def test_replace_still_overwrites_a_destination_created_during_seeding(self):
        real_connect = sqlite3.connect

        def racing_connect(*args, **kwargs):
            self.target.write_bytes(b"created by another process")
            return real_connect(*args, **kwargs)

        with mock.patch.object(seed_module.sqlite3, "connect", racing_connect):
            seed_database(self.target, replace=True)
        con = sqlite3.connect(f"{self.target.as_uri()}?mode=ro", uri=True)
        try:
            self.assertEqual(con.execute("SELECT count(*) FROM orders").fetchone(), (8,))
        finally:
            con.close()
        self.assertEqual(self.names(), [self.target.name])

    def test_temp_file_is_unique_and_beside_the_destination(self):
        real_connect = sqlite3.connect
        used: list[Path] = []

        def spy(path, *args, **kwargs):
            used.append(Path(path))
            return real_connect(path, *args, **kwargs)

        with mock.patch.object(seed_module.sqlite3, "connect", spy):
            seed_database(self.target)
            seed_database(self.target, replace=True)
        self.assertEqual(len(used), 2)
        self.assertNotEqual(used[0], used[1])
        for tmp in used:
            self.assertEqual(tmp.parent, self.target.parent)
            self.assertNotIn(tmp.name, (self.target.name, self.target.name + ".tmp"))

    def test_script_that_leaves_a_transaction_open_is_an_error(self):
        with self.assertRaises(SeedError):
            seed_database(self.target, script="BEGIN; CREATE TABLE t (n);")
        self.assertEqual(list(Path(self._tmp.name).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
