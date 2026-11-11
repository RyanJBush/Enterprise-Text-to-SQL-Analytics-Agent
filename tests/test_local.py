"""Local transport and its normal CLI path, without requiring a model in unit tests."""

import io
import json
import sqlite3
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from tests.helpers import FixtureTestCase
from text2sql.adapter import AdapterError, ScriptedAdapter, build_model_input
from text2sql.cli import main
from text2sql.local import ENDPOINT, MAX_RESPONSE_BYTES, OllamaAdapter

QUESTION = "What was paid revenue by product category in January 2025?"


def response_bytes(reply):
    return json.dumps({
        "done": True,
        "done_reason": "stop",
        "message": {"role": "assistant", "content": json.dumps(reply)},
    }).encode()


def mock_transport(raw):
    response = Mock()
    response.read.return_value = raw
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    opener = Mock()
    opener.open.return_value = response
    return opener


class LocalTransport(unittest.TestCase):
    def test_request_uses_loopback_schema_and_approved_input(self):
        opener = mock_transport(response_bytes({"kind": "sql", "sql": "SELECT 1"}))
        with patch("text2sql.local.build_opener", return_value=opener) as build:
            raw = OllamaAdapter().propose(build_model_input(QUESTION))
        request = opener.open.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, ENDPOINT)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(build.call_args.args[0].proxies, {})
        self.assertEqual(payload["model"], "qwen3:4b")
        self.assertFalse(payload["stream"])
        self.assertTrue(payload["think"])
        self.assertEqual(payload["format"]["type"], "object")
        self.assertNotIn("internal_note", payload["messages"][1]["content"])
        self.assertIn(QUESTION, payload["messages"][1]["content"])
        self.assertEqual(json.loads(raw), {"kind": "sql", "sql": "SELECT 1"})

    def test_unreachable_service_and_missing_model_are_readable_errors(self):
        failures = [URLError("not running"), TimeoutError(),
                    HTTPError(ENDPOINT, 404, "missing", {}, None),
                    HTTPError(ENDPOINT, 500, "failed", {}, None)]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                opener = Mock()
                opener.open.side_effect = failure
                with patch("text2sql.local.build_opener", return_value=opener):
                    with self.assertRaises(AdapterError):
                        OllamaAdapter().propose(build_model_input(QUESTION))

    def test_incomplete_malformed_and_oversized_responses_are_rejected(self):
        cases = [b"broken", b"[]", b"x" * (MAX_RESPONSE_BYTES + 1)]
        for body in [
            {"done": False},
            {"done": True, "done_reason": "length"},
            {"done": True, "done_reason": "stop", "message": {}},
            {"done": True, "done_reason": "stop", "message": {
                "role": "assistant", "content": "{}", "tool_calls": [{}]}},
        ]:
            cases.append(json.dumps(body).encode())
        for raw in cases:
            with self.subTest(raw=raw[:100]):
                with patch("text2sql.local.build_opener", return_value=mock_transport(raw)):
                    with self.assertRaises(AdapterError):
                        OllamaAdapter().propose(build_model_input(QUESTION))


class LocalCli(FixtureTestCase):
    def run_reply(self, reply, **kwargs):
        out = io.StringIO()
        with patch("text2sql.local.build_opener", return_value=mock_transport(response_bytes(reply))):
            code = main(["--local", "--db", str(self.db_path), QUESTION], out=out, **kwargs)
        return code, out.getvalue()

    def test_local_join_uses_the_restricted_executor(self):
        reply = ScriptedAdapter().propose(build_model_input(QUESTION))
        code, output = self.run_reply(reply)
        self.assertEqual(code, 0)
        self.assertIn("LOCAL MODEL qwen3:4b", output)
        self.assertIn("$130.00 for Hardware and $65.00 for Supplies", output)

    def test_local_forbidden_read_is_denied_by_sqlite(self):
        code, output = self.run_reply({"kind": "sql", "sql": "SELECT name FROM sqlite_schema"})
        self.assertEqual(code, 1)
        self.assertIn("SQLITE_READ", output)
        self.assertNotIn("Answer:", output)

    def test_clarification_and_bad_variants_make_zero_executor_calls(self):
        cases = [({"kind": "clarify", "question": "Which measure?"}, 0),
                 ({"kind": "sql", "question": "Wrong variant"}, 2)]
        for reply, expected in cases:
            with self.subTest(reply=reply):
                executor = Mock(side_effect=AssertionError("executor called"))
                with patch.object(sqlite3, "connect", side_effect=AssertionError("database opened")):
                    code, _ = self.run_reply(reply, execute=executor)
                self.assertEqual(code, expected)
                executor.assert_not_called()

    def test_local_and_hosted_flags_cannot_be_combined(self):
        with patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(main(["--local", "--live"]), 2)


if __name__ == "__main__":
    unittest.main()
