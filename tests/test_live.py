"""The opt-in live adapter, tested with a fake client. No network, no API key, no SDK needed.

What this proves: request shape, reply handling, and that a live reply travels the SAME
validated sql/clarify contract and restricted executor as the scripted one.
What it cannot prove: how a real model behaves. See review/live-transcripts.txt for that.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.helpers import FixtureTestCase
from tests.test_cli import (
    CROSS7, Q_AMBIGUOUS, Q_FORBIDDEN, Q_JOINED, ROOT, ExecutorSpy,
)
from text2sql import cli
from text2sql.adapter import AdapterError, ScriptedAdapter, build_model_input, parse_response
from text2sql.live import DEFAULT_MODEL, REPLY_SCHEMA, SYSTEM_PROMPT, AnthropicAdapter

JOINED_SQL = ScriptedAdapter().propose(build_model_input(Q_JOINED))["sql"]


def text(t):
    return SimpleNamespace(type="text", text=t)


def response(*blocks, stop_reason="end_turn", stop_details=None):
    return SimpleNamespace(content=list(blocks), stop_reason=stop_reason, stop_details=stop_details)


def reply(**fields):
    return response(text(json.dumps(fields)))


class FakeClient:
    def __init__(self, outcome):
        self.calls: list[dict] = []
        outer = self

        class Messages:
            def create(self, **kwargs):
                outer.calls.append(kwargs)
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome

        self.messages = Messages()


def adapter_for(outcome, **kwargs):
    client = FakeClient(outcome)
    return AnthropicAdapter(client=client, **kwargs), client


class RequestShape(unittest.TestCase):
    def setUp(self):
        self.adapter, self.client = adapter_for(reply(kind="clarify", question="Which?"))
        self.adapter.propose(build_model_input(Q_JOINED))
        (self.request,) = self.client.calls

    def test_model_gets_question_approved_schema_and_business_definitions(self):
        (message,) = self.request["messages"]
        self.assertEqual(message["role"], "user")
        self.assertEqual(message["content"], build_model_input(Q_JOINED).render())
        for needle in (Q_JOINED, "orders(", "products(", "exclusive end", "quantity * unit_price_cents",
                       "integer USD cents", "orders.product_id to products.product_id"):
            self.assertIn(needle, message["content"])

    def test_private_column_is_never_sent(self):
        self.assertNotIn("internal_note", json.dumps(self.request))

    def test_output_is_pinned_to_the_reply_schema(self):
        self.assertEqual(self.request["output_config"], {"format": {"type": "json_schema", "schema": REPLY_SCHEMA}})
        self.assertIs(REPLY_SCHEMA["additionalProperties"], False)
        self.assertEqual(REPLY_SCHEMA["properties"]["kind"]["enum"], ["sql", "clarify"])

    def test_only_parameters_current_models_accept(self):
        self.assertEqual(
            set(self.request), {"model", "max_tokens", "system", "messages", "output_config"}
        )  # no temperature/top_p/top_k, tool_choice, thinking override or prefill
        self.assertEqual(self.request["model"], DEFAULT_MODEL)
        self.assertEqual(self.request["system"], SYSTEM_PROMPT)
        self.assertGreaterEqual(self.request["max_tokens"], 16000)

    def test_model_id_is_configurable_and_shown_in_the_label(self):
        adapter, client = adapter_for(reply(kind="clarify", question="?"), model="claude-sonnet-5-5")
        adapter.propose(build_model_input("q"))
        self.assertEqual(client.calls[0]["model"], "claude-sonnet-5-5")
        self.assertIn("LIVE MODEL claude-sonnet-5-5", adapter.label)
        self.assertIn("untrusted", adapter.label)

    def test_the_prompt_tells_the_model_the_question_is_untrusted_and_to_clarify_when_ambiguous(self):
        self.assertIn("untrusted", SYSTEM_PROMPT)
        self.assertIn("clarify", SYSTEM_PROMPT)
        self.assertIn("nothing else", SYSTEM_PROMPT)  # JSON only: no narrative, no answer text


class ReplyHandling(unittest.TestCase):
    def propose(self, outcome):
        adapter, _ = adapter_for(outcome)
        return adapter.propose(build_model_input("q"))

    def test_text_reply_is_returned_verbatim_for_the_shared_validator(self):
        raw = json.dumps({"kind": "sql", "sql": "SELECT 1"})
        self.assertEqual(self.propose(response(text(raw))), raw)
        self.assertEqual(parse_response(raw).sql, "SELECT 1")

    def test_thinking_blocks_are_ignored(self):
        raw = '{"kind":"sql","sql":"SELECT 1"}'
        thinking = SimpleNamespace(type="thinking", thinking="")
        self.assertEqual(self.propose(response(thinking, text(raw))), raw)

    def test_everything_unusable_is_an_adapter_error(self):
        cases = {
            "refusal": response(stop_reason="refusal", stop_details=SimpleNamespace(category="cyber")),
            "refusal without details": response(stop_reason="refusal"),
            "cut off": response(text('{"kind":"sql","sql":"SEL'), stop_reason="max_tokens"),
            "no content": response(),
            "two text blocks": response(text("{}"), text("{}")),
            "tool use": response(SimpleNamespace(type="tool_use", name="x", input={})),
            "tool use plus text": response(text("{}"), SimpleNamespace(type="tool_use")),
        }
        for label, outcome in cases.items():
            with self.subTest(label), self.assertRaises(AdapterError):
                self.propose(outcome)

    def test_refusal_message_carries_the_category(self):
        with self.assertRaisesRegex(AdapterError, r"declined.*cyber"):
            self.propose(response(stop_reason="refusal", stop_details=SimpleNamespace(category="cyber")))

    def test_client_failures_become_adapter_errors_without_leaking_anything_else(self):
        for exc in (RuntimeError("boom"), TimeoutError("slow"), ConnectionError("down")):
            with self.subTest(type(exc).__name__):
                with self.assertRaisesRegex(AdapterError, f"model request failed: {type(exc).__name__}: "):
                    self.propose(exc)

    def test_missing_sdk_is_a_clear_adapter_error(self):
        adapter = AnthropicAdapter()  # constructing must not import the SDK
        with mock.patch.dict(sys.modules, {"anthropic": None}):
            with self.assertRaisesRegex(AdapterError, "not installed"):
                adapter.propose(build_model_input("q"))

    def test_answer_context_is_empty_so_the_answer_comes_only_from_rows(self):
        self.assertEqual(AnthropicAdapter(client=object()).answer_context(Q_JOINED), "")


class LiveThroughTheCli(FixtureTestCase):
    """A fake model, but the real validator, executor, database and CLI."""

    def run_live(self, outcome, question, model=None):
        adapter, client = adapter_for(outcome, **({"model": model} if model else {}))
        spy, out = ExecutorSpy(), io.StringIO()
        code = cli.main([question, "--db", str(self.db_path)], out=out, adapter=adapter, execute=spy)
        return code, out.getvalue(), spy, client

    def test_january_revenue_sql_runs_and_the_answer_is_built_from_rows_only(self):
        code, text_, spy, _ = self.run_live(reply(kind="sql", sql=JOINED_SQL), Q_JOINED)
        self.assertEqual(code, 0, text_)
        self.assertTrue(text_.startswith("LIVE MODEL claude-opus-5-5 - "))
        self.assertNotIn("SCRIPTED", text_)
        self.assertEqual(len(spy.calls), 1)
        self.assertEqual(spy.calls[0][0], JOINED_SQL)
        self.assertIn("Hardware | 13000", text_)
        self.assertTrue(
            text_.endswith("Answer: revenue was $130.00 for Hardware and $65.00 for Supplies.\n"), text_
        )  # no period or 'paid' context: that would have to come from the model

    def test_ambiguous_question_clarifies_and_the_executor_is_called_zero_times(self):
        code, text_, spy, client = self.run_live(
            reply(kind="clarify", question="By revenue or units, and how many?"), Q_AMBIGUOUS
        )
        self.assertEqual((code, spy.calls, len(client.calls)), (0, [], 1))
        self.assertIn("By revenue or units, and how many?", text_)

    def test_a_schema_read_proposed_by_the_model_is_blocked_by_sqlite(self):
        sql = "SELECT name, sql FROM main.sqlite_schema;"
        code, text_, spy, _ = self.run_live(reply(kind="sql", sql=sql), Q_FORBIDDEN)
        self.assertEqual(code, 1)
        self.assertEqual(len(spy.calls), 1, "the model's SQL did reach the executor, which blocked it")
        self.assertIn(sql, text_)
        self.assertIn("Status: denied - blocked by the database (SQLITE_READ on sqlite_master.name)", text_)

    def test_a_model_that_handles_the_forbidden_request_itself_never_reaches_sqlite(self):
        for outcome in (
            reply(kind="clarify", question="I can't show schema definitions; what data do you need?"),
            response(stop_reason="refusal"),
        ):
            with self.subTest(str(outcome)[:50]):
                code, text_, spy, _ = self.run_live(outcome, Q_FORBIDDEN)
                self.assertEqual(spy.calls, [])
                self.assertIn(code, (0, 2))
                self.assertNotIn("Status:", text_)

    def test_model_output_goes_through_the_same_validator(self):
        bad = {
            "fenced json": response(text('```json\n{"kind":"sql","sql":"SELECT 1"}\n```')),
            "prose": response(text("Sure! SELECT 1")),
            "duplicate keys": response(text('{"kind":"clarify","kind":"sql","sql":"SELECT 1"}')),
            "extra field": reply(kind="sql", sql="SELECT 1", note="hi"),
            "both fields": reply(kind="sql", sql="SELECT 1", question="?"),
            "wrong kind": reply(kind="explain", sql="SELECT 1"),
            "empty sql": reply(kind="sql", sql=""),
        }
        for label, outcome in bad.items():
            with self.subTest(label):
                code, text_, spy, _ = self.run_live(outcome, "q")
                self.assertEqual((code, spy.calls), (2, []))
                self.assertIn("Adapter reply rejected", text_)

    def test_hostile_sql_from_the_model_is_stopped_by_the_database_layers(self):
        cases = {
            "denied": "WITH x AS (SELECT 1) DELETE FROM orders",
            "limit_work": CROSS7,
            "invalid_sql": "SELECT 1; DROP TABLE orders",
            "rejected": "DROP TABLE orders",
        }
        for status, sql in cases.items():
            with self.subTest(status):
                code, text_, spy, _ = self.run_live(reply(kind="sql", sql=sql), "q")
                self.assertEqual(code, 1)
                self.assertIn(f"Status: {status}", text_)

    def test_api_failure_is_exit_2_with_nothing_executed(self):
        code, text_, spy, _ = self.run_live(RuntimeError("503"), "q")
        self.assertEqual((code, spy.calls), (2, []))
        self.assertIn("Adapter error: model request failed", text_)


class MissingCredentials(unittest.TestCase):
    def test_no_credentials_tells_the_user_what_to_set(self):
        sdk_error = TypeError('"Could not resolve authentication method. Expected one of api_key, ..."')
        adapter, _ = adapter_for(sdk_error)
        with self.assertRaisesRegex(AdapterError, "set ANTHROPIC_API_KEY") as ctx:
            adapter.propose(build_model_input("q"))
        self.assertNotIn("TypeError", str(ctx.exception))


@unittest.skipUnless(importlib.util.find_spec("anthropic") and importlib.util.find_spec("httpx2"),
                     "the real anthropic SDK is not installed (pip install -e .[live])")
class RealSdkContract(unittest.TestCase):
    """Real SDK, canned HTTP transport: proves the SDK accepts our request and parses a reply.
    Still no network and no key; it does NOT prove how a real model behaves."""

    def run_adapter(self, body_text, status=200):
        import anthropic
        import httpx2

        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
            payload = {"id": "msg_test", "type": "message", "role": "assistant", "model": DEFAULT_MODEL,
                       "content": [{"type": "text", "text": body_text}], "stop_reason": "end_turn",
                       "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}}
            return httpx2.Response(status, json=payload)

        client = anthropic.Anthropic(
            api_key="sk-ant-offline-test-key",
            http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)),
        )
        raw = AnthropicAdapter(client=client).propose(build_model_input(Q_AMBIGUOUS))
        return raw, seen

    def test_real_sdk_sends_our_request_and_returns_the_text(self):
        reply_text = json.dumps({"kind": "clarify", "question": "By revenue or units?"})
        raw, seen = self.run_adapter(reply_text)
        self.assertEqual(raw, reply_text)
        self.assertTrue(seen["url"].endswith("/v1/messages"))
        body = seen["body"]
        self.assertEqual(set(body), {"model", "max_tokens", "system", "messages", "output_config"})
        self.assertEqual(body["model"], DEFAULT_MODEL)
        self.assertEqual(body["output_config"]["format"]["schema"], REPLY_SCHEMA)
        self.assertNotIn("internal_note", json.dumps(body))

    def test_real_sdk_with_a_malformed_base_url_is_an_adapter_error(self):
        adapter = AnthropicAdapter()
        with mock.patch.dict(os.environ, {"ANTHROPIC_BASE_URL": "http://exa mple.com/\x1b[2J",
                                          "ANTHROPIC_API_KEY": "sk-ant-offline-test-key"}):
            with self.assertRaisesRegex(AdapterError, r"could not create the model client \(InvalidURL\)"):
                adapter.propose(build_model_input("q"))

    def test_real_sdk_without_credentials_gives_the_actionable_message(self):
        env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
        env["ANTHROPIC_BASE_URL"] = "https://api.anthropic.com"
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(AdapterError, "set ANTHROPIC_API_KEY"):
                AnthropicAdapter().propose(build_model_input("q"))


class InvalidURL(Exception):
    """Stands in for the real SDK's InvalidURL (raised for a malformed ANTHROPIC_BASE_URL)."""


def stub_sdk(**attrs):
    return mock.patch.dict(sys.modules, {"anthropic": SimpleNamespace(**attrs)})


class ClientInitFailure(FixtureTestCase):
    SECRET_URL = "https://user:hunter2@exa mple.com/\x1b[2J"

    def failing_init(self, **kwargs):
        raise InvalidURL(f"Invalid URL {self.SECRET_URL!r}")

    def test_init_failure_is_an_adapter_error_not_an_exception(self):
        adapter = AnthropicAdapter()
        with stub_sdk(Anthropic=self.failing_init):
            with self.assertRaisesRegex(AdapterError, r"could not create the model client \(InvalidURL\)"):
                adapter.propose(build_model_input("q"))

    def test_error_text_never_echoes_the_exception_message(self):
        # A malformed base URL can carry credentials; only the exception type is reported.
        adapter = AnthropicAdapter()
        with stub_sdk(Anthropic=self.failing_init):
            with self.assertRaises(AdapterError) as ctx:
                adapter.propose(build_model_input("q"))
        self.assertNotIn("hunter2", str(ctx.exception))
        self.assertNotIn("\x1b", str(ctx.exception))

    def test_cli_exits_2_with_a_readable_error_and_zero_executor_calls(self):
        spy, out = ExecutorSpy(), io.StringIO()
        with stub_sdk(Anthropic=self.failing_init), \
                mock.patch.object(sqlite3, "connect", side_effect=AssertionError("database touched")):
            code = cli.main(["--live", "--db", str(self.db_path), "q"], out=out, execute=spy)
        text_ = out.getvalue()
        self.assertEqual((code, spy.calls), (2, []))
        self.assertIn("Adapter error: could not create the model client (InvalidURL)", text_)
        self.assertIn("check ANTHROPIC_BASE_URL", text_)
        self.assertIn("Nothing was executed", text_)
        self.assertNotIn("Traceback", text_)
        self.assertNotIn("hunter2", text_)

    def test_real_entry_point_exits_2_without_a_traceback(self):
        # A throwaway `anthropic` package whose constructor fails like the real SDK does.
        with tempfile.TemporaryDirectory() as stub_dir:
            Path(stub_dir, "anthropic.py").write_text(
                "class InvalidURL(Exception):\n    pass\n"
                "def Anthropic(**kwargs):\n"
                "    raise InvalidURL(\"bad url https://user:hunter2@exa mple.com/\")\n",
                encoding="utf-8",
            )
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=stub_dir)
            proc = subprocess.run(
                [sys.executable, "-X", "dev", "-W", "error", "-m", "text2sql.cli", "--live",
                 "--db", str(self.db_path), "What was paid revenue by product category in January 2025?"],
                cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=60,
            )
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertEqual(proc.stderr, "", "no traceback on stderr")
        self.assertIn("could not create the model client (InvalidURL)", proc.stdout)
        self.assertNotIn("Traceback", proc.stdout)
        self.assertNotIn("hunter2", proc.stdout)
        self.assertNotIn("Proposed SQL", proc.stdout)

    def test_a_broken_sdk_install_is_also_an_adapter_error(self):
        class Broken:
            def __getattr__(self, name):
                raise RuntimeError("half-installed")

        adapter = AnthropicAdapter()
        with mock.patch.dict(sys.modules, {"anthropic": Broken()}):
            with self.assertRaises(AdapterError):
                adapter.propose(build_model_input("q"))

    def test_a_working_client_is_created_once_with_a_timeout_and_one_retry(self):
        created = []

        def factory(**kwargs):
            created.append(kwargs)
            return FakeClient(reply(kind="clarify", question="Which?"))

        adapter = AnthropicAdapter()
        with stub_sdk(Anthropic=factory):
            adapter.propose(build_model_input("q"))
            adapter.propose(build_model_input("q"))
        self.assertEqual(created, [{"timeout": 120.0, "max_retries": 1}])


class OptIn(FixtureTestCase):
    def test_default_run_never_loads_the_live_module_or_the_sdk(self):
        code = (
            "import io, sys; from text2sql import cli\n"
            "rc = cli.main([sys.argv[1], '--db', sys.argv[2]], out=io.StringIO())\n"
            "print(rc, 'text2sql.live' in sys.modules, 'anthropic' in sys.modules)\n"
        )
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        proc = subprocess.run([sys.executable, "-c", code, Q_JOINED, str(self.db_path)],
                              cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.stdout.strip(), "0 False False", proc.stderr)

    def test_scripted_paths_open_no_network_connection(self):
        boom = AssertionError("network used by a scripted run")
        with mock.patch.object(socket.socket, "connect", side_effect=boom), \
                mock.patch.object(socket, "getaddrinfo", side_effect=boom), \
                mock.patch("text2sql.live.AnthropicAdapter", side_effect=boom):
            for q in (Q_JOINED, Q_AMBIGUOUS, Q_FORBIDDEN):
                with self.subTest(q):
                    cli.main([q, "--db", str(self.db_path)], out=io.StringIO())

    def test_live_flag_builds_the_live_adapter_with_the_requested_model(self):
        made = []

        def factory(*args, **kwargs):
            made.append((args, kwargs))
            return AnthropicAdapter(*args, client=FakeClient(reply(kind="clarify", question="Which?")), **kwargs)

        for argv, expected in ((["--live"], ()), (["--live", "--model", "claude-sonnet-5-5"], ("claude-sonnet-5-5",))):
            made.clear()
            out = io.StringIO()
            with mock.patch("text2sql.live.AnthropicAdapter", side_effect=factory):
                code = cli.main([*argv, "--db", str(self.db_path), "anything at all"], out=out)
            self.assertEqual(code, 0, out.getvalue())
            self.assertEqual(made, [(expected, {})])
            self.assertTrue(out.getvalue().startswith("LIVE MODEL "))

    def test_model_flag_without_live_is_a_usage_error(self):
        out = io.StringIO()
        self.assertEqual(cli.main(["--model", "x", "--db", str(self.db_path), "q"], out=out), 2)
        self.assertIn("--model only applies with --live", out.getvalue())

    def test_list_questions_stays_scripted_even_with_live(self):
        out = io.StringIO()
        with mock.patch("text2sql.live.AnthropicAdapter", side_effect=AssertionError("built")):
            self.assertEqual(cli.main(["--live", "--list-questions"], out=out), 0)
        self.assertTrue(out.getvalue().startswith("SCRIPTED DEMO"))

    def test_live_without_the_sdk_or_credentials_is_exit_2_not_a_traceback(self):
        out = io.StringIO()
        with mock.patch.dict(sys.modules, {"anthropic": None}):
            code = cli.main(["--live", "--db", str(self.db_path), "q"], out=out)
        self.assertEqual(code, 2)
        self.assertIn("not installed", out.getvalue())


if __name__ == "__main__":
    unittest.main()
