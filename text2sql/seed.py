"""Seed a fresh SQLite fixture from the checked-in ``sql/seed.sql``.

    python -m text2sql.seed C:\\abs\\path\\fixture.sqlite3 [--replace]

The fixture is built in a uniquely named temporary file in the destination directory
and published atomically only after the seed transaction committed and
``PRAGMA foreign_key_check`` came back empty. Without ``--replace`` publishing fails if
the destination exists, even if it appeared while seeding. Only this run's own temporary
file is ever removed; other files, including a ``<path>.tmp`` sibling, are left alone.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

SEED_SQL_PATH = Path(__file__).resolve().parent.parent / "sql" / "seed.sql"


class SeedError(RuntimeError):
    """The fixture could not be built or failed verification."""


def seed_database(
    path: str | os.PathLike[str],
    *,
    replace: bool = False,
    script: str | None = None,
) -> Path:
    """Build the fixture at ``path`` and return it.

    ``script`` overrides the checked-in SQL; it exists so tests can prove that a
    failing seed rolls back and leaves nothing behind.
    """
    final = Path(path)
    if not final.is_absolute():
        raise ValueError(f"fixture path must be absolute, got {final!s}")
    if final.exists() and not replace:
        raise FileExistsError(f"{final} already exists; pass replace=True to rebuild it")

    sql = SEED_SQL_PATH.read_text(encoding="utf-8") if script is None else script
    # A unique temp file created by THIS run, next to the destination (same filesystem, so
    # publishing is atomic). Never a predictable name: that could belong to someone else.
    fd, tmp_name = tempfile.mkstemp(dir=final.parent, prefix=f".{final.name}.", suffix=".tmp")
    os.close(fd)
    tmp = Path(tmp_name)

    try:
        _build(tmp, sql)
        _publish(tmp, final, replace=replace)
    finally:
        tmp.unlink(missing_ok=True)  # only ever our own temp file
    return final


def _publish(tmp: Path, final: Path, *, replace: bool) -> None:
    """Move the finished fixture to ``final`` atomically.

    Without ``replace`` this must fail if ``final`` exists, including if it appeared after
    the caller's early check, so it never relies on check-then-rename.
    """
    if replace:
        os.replace(tmp, final)  # explicit overwrite, atomic
    elif os.name == "nt":
        os.rename(tmp, final)  # Windows: raises FileExistsError if final exists
    else:
        os.link(tmp, final)  # POSIX: raises FileExistsError if final exists; caller unlinks tmp


def _build(tmp: Path, sql: str) -> None:
    # isolation_level=None: no implicit transactions; the script owns BEGIN/COMMIT.
    con = sqlite3.connect(tmp, isolation_level=None)
    try:
        # Must happen before the script's BEGIN: the pragma is a no-op inside a transaction.
        con.execute("PRAGMA foreign_keys=ON")
        if con.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise SeedError("could not enable foreign key enforcement")

        con.executescript(sql)
        if con.in_transaction:
            raise SeedError("seed script left a transaction open")

        violations = con.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise SeedError(f"foreign_key_check returned rows: {violations!r}")
    finally:
        con.close()  # must be closed before the file is renamed (Windows)


def _count(path: Path, table: str) -> int:
    con = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        return con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed the text-to-SQL fixture database.")
    parser.add_argument("path", type=Path, help="absolute path of the SQLite file to create")
    parser.add_argument("--replace", action="store_true", help="overwrite an existing file")
    args = parser.parse_args(argv)
    try:
        path = seed_database(args.path.absolute(), replace=args.replace)
    except (FileExistsError, SeedError, sqlite3.Error) as exc:
        print(f"seed failed: {exc}", file=sys.stderr)
        return 1
    print(f"seeded {path}: {_count(path, 'products')} products, {_count(path, 'orders')} orders")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
