"""Restricted, read-only SQLite executor.

Defence in depth, outermost first:

1. Input gate      first token must be SELECT or WITH (``check_input``).
2. File mode       ``mode=ro`` URI on an application-owned absolute path.
3. Pragmas         ``query_only=ON``, ``trusted_schema=OFF``, ``temp_store=MEMORY``.
4. Authorizer      deny by default; allow only approved reads, SELECT, 6 functions.
5. SQLite limits   SQL length, value size, columns, expression depth, compound terms.
6. Work budget     progress handler: ~100k VM instructions or a 250 ms deadline.
7. Output cap      at most 100 rows; 101 rows means a row-limit status, no rows.

Every request gets a fresh connection that is closed on every path.
"""

from __future__ import annotations

import enum
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .schema import ALLOWED_FUNCTIONS, APPROVED_COLUMNS

# ---------------------------------------------------------------- configuration
MAX_SQL_BYTES = 8 * 1024
MAX_VALUE_BYTES = 64 * 1024  # SQLITE_LIMIT_LENGTH: any string, blob or row
MAX_COLUMNS = 12
MAX_EXPR_DEPTH = 50
MAX_COMPOUND_TERMS = 8
BUSY_TIMEOUT_S = 0.050
PROGRESS_INTERVAL = 1_000  # VM instructions between progress callbacks
MAX_VM_INSTRUCTIONS = 100_000
DEADLINE_S = 0.250
MAX_ROWS = 100

_LIMITS: tuple[tuple[int, int], ...] = (
    (sqlite3.SQLITE_LIMIT_SQL_LENGTH, MAX_SQL_BYTES),
    (sqlite3.SQLITE_LIMIT_LENGTH, MAX_VALUE_BYTES),
    (sqlite3.SQLITE_LIMIT_COLUMN, MAX_COLUMNS),
    (sqlite3.SQLITE_LIMIT_EXPR_DEPTH, MAX_EXPR_DEPTH),
    (sqlite3.SQLITE_LIMIT_COMPOUND_SELECT, MAX_COMPOUND_TERMS),
)
_PRAGMAS: tuple[tuple[str, str, int], ...] = (
    # (name, value to set, value expected back)
    ("query_only", "ON", 1),
    ("trusted_schema", "OFF", 0),
    ("temp_store", "MEMORY", 2),
)

# SQLite reports these limit violations as plain SQLITE_ERROR, so match the message.
_SIZE_LIMIT_MESSAGES = (
    "too many columns",
    "expression tree is too large",
    "too many terms in compound select",
    "too many terms in order by",
    "too many terms in group by",
    "string or blob too big",
    "statement too long",
)
# Errors Python itself raises (not SQLite) that mean "your SQL is not one statement".
_PY_INVALID_MESSAGES = ("one statement at a time", "null character")


class ExecutorConfigError(RuntimeError):
    """The restricted connection could not be configured as required (fail closed)."""


# --------------------------------------------------------------------- results
class Status(str, enum.Enum):
    OK = "ok"
    REJECTED = "rejected"  # failed the input gate; no connection was opened
    DENIED = "denied"  # SQLite's authorizer blocked it
    INVALID_SQL = "invalid_sql"  # syntax error, unknown name, more than one statement
    LIMIT_WORK = "limit_work"  # VM-instruction or time budget exhausted
    LIMIT_SIZE = "limit_size"  # SQL/value/columns/depth/compound limit exceeded
    LIMIT_ROWS = "limit_rows"  # more than MAX_ROWS rows; no partial answer
    ERROR = "error"  # anything unexpected (unreadable file, busy, ...)


STATUS_MESSAGES: dict[Status, str] = {
    Status.OK: "Query ran.",
    Status.REJECTED: "Only a single SELECT or WITH statement is accepted.",
    Status.DENIED: "Blocked: this query reads or does something that is not allowed.",
    Status.INVALID_SQL: "The SQL is not valid.",
    Status.LIMIT_WORK: "Stopped: the query used too much work.",
    Status.LIMIT_SIZE: "Stopped: the query or one of its values is too large.",
    Status.LIMIT_ROWS: f"More than {MAX_ROWS} rows matched; narrow the question.",
    Status.ERROR: "The database could not run this query.",
}

_ACTION_NAMES = {
    getattr(sqlite3, name): name
    for name in (
        "SQLITE_CREATE_INDEX", "SQLITE_CREATE_TABLE", "SQLITE_CREATE_TEMP_INDEX",
        "SQLITE_CREATE_TEMP_TABLE", "SQLITE_CREATE_TEMP_TRIGGER", "SQLITE_CREATE_TEMP_VIEW",
        "SQLITE_CREATE_TRIGGER", "SQLITE_CREATE_VIEW", "SQLITE_DELETE", "SQLITE_DROP_INDEX",
        "SQLITE_DROP_TABLE", "SQLITE_DROP_TEMP_INDEX", "SQLITE_DROP_TEMP_TABLE",
        "SQLITE_DROP_TEMP_TRIGGER", "SQLITE_DROP_TEMP_VIEW", "SQLITE_DROP_TRIGGER",
        "SQLITE_DROP_VIEW", "SQLITE_INSERT", "SQLITE_PRAGMA", "SQLITE_READ", "SQLITE_SELECT",
        "SQLITE_TRANSACTION", "SQLITE_UPDATE", "SQLITE_ATTACH", "SQLITE_DETACH",
        "SQLITE_ALTER_TABLE", "SQLITE_REINDEX", "SQLITE_ANALYZE", "SQLITE_CREATE_VTABLE",
        "SQLITE_DROP_VTABLE", "SQLITE_FUNCTION", "SQLITE_SAVEPOINT", "SQLITE_RECURSIVE",
    )
    if hasattr(sqlite3, name)
}


@dataclass(frozen=True)
class Denial:
    """The first authorizer callback that said no (what SQLite asked, verbatim)."""

    action: int
    arg1: str | None
    arg2: str | None
    dbname: str | None
    source: str | None

    @property
    def action_name(self) -> str:
        return _ACTION_NAMES.get(self.action, f"action_{self.action}")


@dataclass
class QueryResult:
    status: Status
    columns: tuple[str, ...] = ()
    rows: list[tuple] = field(default_factory=list)
    detail: str = ""  # raw reason, for logs and review; not for end users
    denial: Denial | None = None

    @property
    def message(self) -> str:
        return STATUS_MESSAGES[self.status]


# ------------------------------------------------------------------ input gate
_SQL_WHITESPACE = " \t\n\f\r"  # exactly what SQLite treats as whitespace


def _continues_identifier(ch: str) -> bool:
    return bool(ch) and (ch.isascii() and (ch.isalnum() or ch in "_$") or not ch.isascii())


def check_input(sql: object) -> str | None:
    """Return None if ``sql`` may reach the database, else the reason it may not.

    Only the first token is examined. Leading comments, parentheses, EXPLAIN,
    VALUES and everything else are outside the MVP input format.
    """
    if not isinstance(sql, str):
        return "SQL must be a string."
    body = sql.lstrip(_SQL_WHITESPACE)
    for keyword in ("SELECT", "WITH"):
        head = body[: len(keyword)]
        # isascii(): str.upper() maps U+017F (long s) to "S", which is not SQL.
        if head.isascii() and head.upper() == keyword:
            if not _continues_identifier(body[len(keyword) : len(keyword) + 1]):
                return None
    return STATUS_MESSAGES[Status.REJECTED]


# ------------------------------------------------------------------ authorizer
class Authorizer:
    """Deny-by-default ``sqlite3`` authorizer that remembers its first denial."""

    def __init__(self) -> None:
        self.denial: Denial | None = None

    def reset(self) -> None:
        self.denial = None

    @staticmethod
    def allows(
        action: int, arg1: str | None, arg2: str | None, dbname: str | None
    ) -> bool:
        if action == sqlite3.SQLITE_SELECT:
            return True
        if action == sqlite3.SQLITE_READ:
            columns = APPROVED_COLUMNS.get((arg1 or "").lower())
            if columns is None:
                return False
            if arg2 == "":
                # SQLite's "no particular column" read (e.g. COUNT(*)). dbname can be
                # None here; that is safe only because ATTACH and CREATE TEMP are denied,
                # so the fresh connection has no database other than main.
                return dbname in ("main", None)
            return dbname == "main" and (arg2 or "").lower() in columns
        if action == sqlite3.SQLITE_FUNCTION:
            return (arg2 or "").lower() in ALLOWED_FUNCTIONS
        return False

    def __call__(
        self,
        action: int,
        arg1: str | None,
        arg2: str | None,
        dbname: str | None,
        source: str | None,
    ) -> int:
        try:
            allowed = self.allows(action, arg1, arg2, dbname)
        except Exception:  # never let a bug here turn into an allow
            allowed = False
        if allowed:
            return sqlite3.SQLITE_OK
        if self.denial is None:
            self.denial = Denial(action, arg1, arg2, dbname, source)
        return sqlite3.SQLITE_DENY


# ------------------------------------------------------------------ connection
def _require_absolute(db_path: str | Path) -> Path:
    path = Path(db_path)
    if not path.is_absolute():
        raise ValueError(f"db_path must be an application-owned absolute path, got {path!s}")
    return path


class RestrictedDb:
    """One fully restricted connection. Construct per request and close it.

    ``run`` does NOT apply the input gate; ``execute_query`` does. That split lets
    tests aim statements the gate would turn away straight at the authorizer.
    """

    def __init__(
        self, db_path: str | Path, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        path = _require_absolute(db_path)
        self._clock = clock
        self._authorizer = Authorizer()
        self._ticks = 0
        self._deadline = 0.0
        self.tripped: str | None = None  # "vm_instructions" | "deadline" | None
        self.conn = sqlite3.connect(
            f"{path.as_uri()}?mode=ro",
            uri=True,
            timeout=BUSY_TIMEOUT_S,
            isolation_level=None,
            cached_statements=0,
        )
        try:
            self._configure()
        except BaseException:
            self.conn.close()
            raise

    def _configure(self) -> None:
        con = self.conn
        # Trusted setup first, while no authorizer is installed.
        if hasattr(con, "enable_load_extension"):
            con.enable_load_extension(False)
        for name, value, expected in _PRAGMAS:
            con.execute(f"PRAGMA {name}={value}")
            actual = con.execute(f"PRAGMA {name}").fetchone()[0]
            if actual != expected:
                raise ExecutorConfigError(f"PRAGMA {name} is {actual!r}, expected {expected!r}")
        for category, value in _LIMITS:
            con.setlimit(category, value)
            if con.getlimit(category) != value:
                raise ExecutorConfigError(f"SQLite limit {category} not applied")
        con.set_authorizer(self._authorizer)
        con.set_progress_handler(self._progress, PROGRESS_INTERVAL)

    @property
    def progress_ticks(self) -> int:
        """Progress callbacks seen by the current/last ``run`` (1 tick = 1,000 VM instructions)."""
        return self._ticks

    def _progress(self) -> int:
        self._ticks += 1
        if self._ticks * PROGRESS_INTERVAL >= MAX_VM_INSTRUCTIONS:
            self.tripped = "vm_instructions"
            return 1
        if self._clock() >= self._deadline:
            self.tripped = "deadline"
            return 1
        return 0

    def run(self, sql: str) -> QueryResult:
        self._authorizer.reset()
        self.tripped = None
        self._ticks = 0
        self._deadline = self._clock() + DEADLINE_S
        cur = None
        try:
            # The progress handler stays installed through execute() AND fetching.
            cur = self.conn.execute(sql)
            columns = tuple(d[0] for d in cur.description or ())
            fetched = cur.fetchmany(MAX_ROWS + 1)
        except (sqlite3.Error, sqlite3.Warning, ValueError) as exc:
            return self._classify(exc)
        finally:
            if cur is not None:
                cur.close()
        if len(fetched) > MAX_ROWS:
            return QueryResult(
                Status.LIMIT_ROWS, columns=columns, detail=f"more than {MAX_ROWS} rows"
            )
        return QueryResult(Status.OK, columns=columns, rows=fetched)

    def _classify(self, exc: Exception) -> QueryResult:
        text = str(exc)
        lowered = text.lower()
        code = getattr(exc, "sqlite_errorcode", None)
        primary = None if code is None else code & 0xFF

        denial = self._authorizer.denial
        # Most denials fail with SQLITE_AUTH, but a denied *function* surfaces as a plain
        # SQLITE_ERROR ("not authorized to use function: ..."). A recorded denial means the
        # authorizer is what stopped the statement, whatever code SQLite chose.
        if primary == sqlite3.SQLITE_AUTH or (
            denial is not None and primary == sqlite3.SQLITE_ERROR
        ):
            return QueryResult(Status.DENIED, detail=text, denial=denial)
        if primary == sqlite3.SQLITE_INTERRUPT and self.tripped:
            return QueryResult(Status.LIMIT_WORK, detail=f"budget exhausted: {self.tripped}")
        # DataError covers SQLITE_TOOBIG and Python's own "query string is too large".
        if isinstance(exc, sqlite3.DataError) or primary == sqlite3.SQLITE_TOOBIG:
            return QueryResult(Status.LIMIT_SIZE, detail=text)
        if primary == sqlite3.SQLITE_ERROR:
            if any(m in lowered for m in _SIZE_LIMIT_MESSAGES):
                return QueryResult(Status.LIMIT_SIZE, detail=text)
            return QueryResult(Status.INVALID_SQL, detail=text)
        if any(m in lowered for m in _PY_INVALID_MESSAGES):
            return QueryResult(Status.INVALID_SQL, detail=text)
        return QueryResult(Status.ERROR, detail=f"{type(exc).__name__}: {text}")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "RestrictedDb":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def execute_query(
    sql: str,
    *,
    db_path: str | Path,
    clock: Callable[[], float] = time.monotonic,
) -> QueryResult:
    """Gate, then run ``sql`` on a fresh restricted connection that is always closed."""
    path = _require_absolute(db_path)
    reason = check_input(sql)
    if reason is not None:
        return QueryResult(Status.REJECTED, detail=reason)
    try:
        db = RestrictedDb(path, clock=clock)
    except (sqlite3.Error, ExecutorConfigError) as exc:
        return QueryResult(Status.ERROR, detail=f"{type(exc).__name__}: {exc}")
    with db:
        return db.run(sql)
