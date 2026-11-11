"""Safe terminal display for untrusted text.

A model, a question, a SQL comment, a column alias or a database value can all carry
escape sequences (ANSI clear-screen, cursor moves, bidi overrides). Anything untrusted is
rendered through ``escape_controls`` so it shows up as visible text instead of acting on
the terminal. This only affects what is printed; the original strings are never altered.
"""

from __future__ import annotations

import unicodedata

_NAMED = {"\t": "\\t", "\n": "\\n", "\r": "\\r"}

# Control, format (incl. bidi overrides, zero-width), surrogate, private-use, unassigned,
# and the line/paragraph separators.
_UNSAFE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})


def _escape(ch: str) -> str:
    if ch in _NAMED:
        return _NAMED[ch]
    code = ord(ch)
    if code <= 0xFF:
        return f"\\x{code:02x}"
    if code <= 0xFFFF:
        return f"\\u{code:04x}"
    return f"\\U{code:08x}"


def escape_controls(text: str) -> str:
    """Return ``text`` with every control-like character replaced by a visible escape.

    Newlines are escaped too: callers that want multi-line output split on ``\\n`` first
    and escape each line.
    """
    if text.isascii() and text.isprintable():
        return text  # the common case
    return "".join(
        _escape(ch) if ch in _NAMED or unicodedata.category(ch) in _UNSAFE_CATEGORIES else ch
        for ch in text
    )


def escape_lines(text: str) -> str:
    """Like ``escape_controls`` but keeps ``\\n`` as the line structure (help, usage, errors)."""
    return "\n".join(escape_controls(line) for line in text.split("\n"))
