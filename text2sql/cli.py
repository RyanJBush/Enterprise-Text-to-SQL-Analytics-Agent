"""Command-line demo: question -> validated adapter reply -> restricted executor -> answer.

    python -m text2sql.cli --db C:\\abs\\fixture.sqlite3 "What was paid revenue by product category in January 2025?"
    python -m text2sql.cli --list-questions
    python -m text2sql.cli --local --db fixture.sqlite3 "any question"           # local Ollama
    python -m text2sql.cli --live --db C:\\abs\\fixture.sqlite3 "any question"     # opt-in, calls a model

The default adapter is scripted; --local uses Ollama and --live uses Anthropic. Each transcript's first
line says which adapter produced the SQL. Exit codes: 0 answered or asked to clarify,
1 the database refused or limited the query, 2 bad input or an unusable adapter reply
(nothing was executed).

Everything printed passes through ``escape_controls``: questions, SQL, model text, column
names, row values and diagnostics are untrusted, and control characters are shown as
visible escapes instead of reaching the terminal. The SQL given to SQLite is untouched.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TextIO

from .adapter import (
    Adapter,
    AdapterError,
    Clarification,
    ScriptedAdapter,
    build_model_input,
    parse_response,
)
from .answer import summarize
from .display import escape_controls, escape_lines
from .executor import QueryResult, Status, execute_query

EXIT_OK, EXIT_REFUSED, EXIT_USAGE = 0, 1, 2

Executor = Callable[..., QueryResult]


def _table(columns: Sequence[str], rows: Sequence[Sequence[object]]) -> list[str]:
    # Escape BEFORE measuring so column widths match what is actually printed.
    head = [escape_controls(c) for c in columns]
    cells = [[("NULL" if v is None else escape_controls(str(v))) for v in row] for row in rows]
    widths = [max([len(c)] + [len(r[i]) for r in cells]) for i, c in enumerate(head)]
    line = lambda parts: ("  " + " | ".join(p.ljust(w) for p, w in zip(parts, widths))).rstrip(" ")
    out = [line(head), "  " + "-+-".join("-" * w for w in widths)]
    out += [line(r) for r in cells]
    return out


def _block(text: str) -> list[str]:
    # Split on "\n" only: str.splitlines() would also split on \r, \x0b, \x1c.. and hide them.
    # Escape first, then trim only spaces: rstrip() on raw text would silently eat a trailing
    # \r, \t, \x0b ... and hide it from the reader.
    return [("  " + escape_controls(line)).rstrip(" ") for line in text.split("\n")]


def _status_line(result: QueryResult) -> str:
    if result.status is Status.DENIED:
        d = result.denial
        what = "the database refused it"
        if d is not None:
            target = ".".join(p for p in (d.arg1, d.arg2) if p)
            what = f"{d.action_name} on {target}" if target else d.action_name
        return f"Status: denied - blocked by the database ({what}). No rows were returned."
    return f"Status: {result.status.value} - {result.message}"


def _build_adapter(args: argparse.Namespace) -> Adapter:
    if args.local:
        from .local import OllamaAdapter

        return OllamaAdapter(args.model) if args.model else OllamaAdapter()
    if not args.live:
        return ScriptedAdapter()
    from .live import AnthropicAdapter  # imported only on --live

    return AnthropicAdapter(args.model) if args.model else AnthropicAdapter()


class _SafeParser(argparse.ArgumentParser):
    """argparse echoes user-supplied option text in errors and writes it straight to
    stderr. Route everything it prints (errors, usage, help) through the escaper, keeping
    newlines, so help formatting is unchanged and an ESC in an option cannot reach the terminal."""

    def _print_message(self, message: str, file: TextIO | None = None) -> None:
        if message:
            try:
                (file or sys.stderr).write(escape_lines(message))
            except (AttributeError, OSError):
                pass


def _make_parser() -> argparse.ArgumentParser:
    parser = _SafeParser(prog="text2sql.cli", description=__doc__.split("\n")[0])
    parser.add_argument("question", nargs="?", help="the question to ask")
    parser.add_argument("--db", type=Path, help="path to the seeded fixture database")
    parser.add_argument("--list-questions", action="store_true", help="show the scripted questions")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--local", action="store_true",
                      help="use a local Ollama model (default: qwen3:4b; no API key needed)")
    mode.add_argument("--live", action="store_true",
                        help="OPT IN: ask a live model instead of the script (needs the anthropic package and credentials)")
    parser.add_argument("--model", help="model ID for --local or --live (default: the adapter's default)")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    out: TextIO | None = None,
    adapter: Adapter | None = None,
    execute: Executor = execute_query,
) -> int:
    out = out or sys.stdout
    # Every line goes through the escaper, so no code path can print a raw control character.
    say = lambda *lines: print(*(escape_controls(line) for line in lines), sep="\n", file=out)

    parser = _make_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse exits itself on bad usage; keep that as our code 2
        return EXIT_USAGE if exc.code not in (0, None) else EXIT_OK

    if args.list_questions:
        scripted = ScriptedAdapter()
        say(scripted.label, *(f"  {case.question}" for case in scripted.cases))
        return EXIT_OK
    if args.model and not (args.live or args.local):
        say("Usage error: --model only applies with --live or --local.")
        return EXIT_USAGE
    if adapter is None:
        adapter = _build_adapter(args)

    say(adapter.label)
    if not args.question or args.db is None:
        say("Usage error: give a question and --db PATH (or --list-questions).")
        return EXIT_USAGE

    say(f"Question: {args.question}", "")

    try:
        raw = adapter.propose(build_model_input(args.question))
    except AdapterError as exc:
        say(f"Adapter error: {exc}. Nothing was executed.")
        return EXIT_USAGE
    try:
        proposal = parse_response(raw)  # validate BEFORE anything can reach the executor
    except AdapterError as exc:
        say(f"Adapter reply rejected: {exc}. Nothing was executed.")
        return EXIT_USAGE

    if isinstance(proposal, Clarification):
        say("Clarification needed. No SQL was run.", *_block(proposal.question))
        return EXIT_OK

    say("Proposed SQL:", *_block(proposal.sql), "")
    result = execute(proposal.sql, db_path=args.db.absolute())  # the original, unescaped SQL
    say(_status_line(result))
    if result.status is not Status.OK:
        if result.status is not Status.DENIED and result.detail:
            say(f"Detail: {result.detail}")
        return EXIT_REFUSED

    if result.rows:
        say(f"Rows ({len(result.rows)}):", *_table(result.columns, result.rows))
    else:
        say("Rows (0): none")
    say("", "Answer: " + summarize(result.columns, result.rows, adapter.answer_context(args.question)))
    return EXIT_OK


def run() -> int:
    """Entry point for ``python -m text2sql.cli``: never crash on an unencodable character."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="backslashreplace")
    return main()


if __name__ == "__main__":
    raise SystemExit(run())
