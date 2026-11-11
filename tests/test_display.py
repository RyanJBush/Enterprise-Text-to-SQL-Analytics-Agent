"""escape_controls: control-like characters become visible; everything else is untouched."""

from __future__ import annotations

import unittest

from text2sql.display import escape_controls


class EscapeControls(unittest.TestCase):
    ESCAPED = {
        "\x1b": "\\x1b",  # ESC: starts every ANSI sequence
        "\x00": "\\x00",
        "\x07": "\\x07",  # bell
        "\x08": "\\x08",  # backspace
        "\x7f": "\\x7f",  # DEL
        "\x9b": "\\x9b",  # C1 CSI: an 8-bit ANSI introducer
        "\x85": "\\x85",  # C1 NEL
        "\t": "\\t",
        "\n": "\\n",
        "\r": "\\r",
        "‮": "\\u202e",  # right-to-left override
        "⁦": "\\u2066",  # bidi isolate
        " ": "\\u2028",  # line separator
        " ": "\\u2029",
        "​": "\\u200b",  # zero-width space
        "﻿": "\\ufeff",  # BOM / zero-width no-break space
        "­": "\\xad",  # soft hyphen
        "\U000e0001": "\\U000e0001",  # tag character
        "\ud800": "\\ud800",  # lone surrogate (would crash print on a utf-8 stream)
        "": "\\ue000",  # private use
    }
    KEPT = ["", "plain text", "SELECT * FROM orders WHERE x = 'a b';", "café", "中文", "\U0001f600",
            "a\\b", " ", "~", " "]

    def test_each_control_like_character_is_made_visible(self):
        for ch, expected in self.ESCAPED.items():
            with self.subTest(char=expected):
                self.assertEqual(escape_controls(f"a{ch}b"), f"a{expected}b")

    def test_printable_text_is_left_alone(self):
        for text in self.KEPT:
            with self.subTest(text=text):
                self.assertEqual(escape_controls(text), text)

    def test_a_full_ansi_clear_screen_sequence(self):
        self.assertEqual(escape_controls("\x1b[2J\x1b[H"), "\\x1b[2J\\x1b[H")

    def test_output_never_contains_a_raw_control_character(self):
        nasty = "".join(self.ESCAPED) + "\x1b[2J"
        out = escape_controls(nasty)
        self.assertTrue(out.isascii() and out.isprintable(), out)

    def test_escape_lines_keeps_the_line_structure(self):
        from text2sql.display import escape_lines
        self.assertEqual(escape_lines("usage: x\n\x1b[2Jerror: y\n"), "usage: x\n\\x1b[2Jerror: y\n")
        self.assertEqual(escape_lines("a\r\nb"), "a\\r\nb")
        self.assertEqual(escape_lines(""), "")

    def test_escaping_twice_changes_nothing(self):
        once = escape_controls("x\x1b[2J‮\n")
        self.assertEqual(escape_controls(once), once)


if __name__ == "__main__":
    unittest.main()
