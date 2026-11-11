"""Shared test plumbing: a freshly seeded fixture plus a byte-level integrity check."""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from text2sql.seed import seed_database


def fingerprint(db_path: Path) -> tuple[str, list[str]]:
    """SHA-256 of the fixture bytes plus the directory listing (catches -journal/-wal files)."""
    digest = hashlib.sha256(db_path.read_bytes()).hexdigest()
    return digest, sorted(os.listdir(db_path.parent))


class FixtureTestCase(unittest.TestCase):
    """Seeds one fixture per class and fails any test that changes it."""

    db_path: Path
    _baseline: tuple[str, list[str]]

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.db_path = seed_database(Path(cls._tmp.name) / "fixture.sqlite3")
        cls._baseline = fingerprint(cls.db_path)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def tearDown(self) -> None:
        self.assertEqual(
            fingerprint(self.db_path), self._baseline, "fixture bytes or directory changed"
        )
