"""PiClient against tests/fake_pi_gateway.py (a Python stand-in for the Node gateway), plus the Pi backend wired
through the app: build_services, the approval flow, the screener, titles and `check`."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from subzero_ai.checks import FAIL, OK, run_checks
from subzero_ai.claude import ClaudeError
from subzero_ai.config import Settings, load_settings
from subzero_ai.pi_client import DIED_MESSAGE, DOWN_MESSAGE, PiClient, child_env, key_env_name
from subzero_ai.screener import Screener
from subzero_ai.slack_api import FakeSlack

from .helpers import MENTOR, STUDENT, Harness

FAKE = str(Path(__file__).with_name("fake_pi_gateway.py"))
MSG = [{"role": "user", "content": "hello robot"}]


def make_client(**kwargs) -> PiClient:
    env = kwargs.pop("env", None)
    if env is None:
        env = child_env({"anthropic": "sk-test-key"})
    return PiClient([sys.executable, FAKE], env, **kwargs)


class PiClientCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        logging.disable(logging.NOTSET)  # tests/__init__ turns logging off; these tests check log lines
        self.addCleanup(logging.disable, logging.CRITICAL)

    def client(self, **kwargs) -> PiClient:
        c = make_client(**kwargs)
        self.addAsyncCleanup(c.aclose)
        return c

    async def stream(self, c: PiClient, model: str = "fake/echo", text: str = "hello robot", **kwargs):
        return [e async for e in c.stream(model=model, system="s", messages=[{"role": "user", "content": text}],
                                          max_tokens=100, **kwargs)]

    async def complete(self, c: PiClient, model: str = "fake/echo", text: str = "hello robot", **kwargs) -> str:
        return await c.complete(model=model, system="s", messages=[{"role": "user", "content": text}],
                                max_tokens=100, **kwargs)

    async def seen(self, c: PiClient) -> dict:
        return json.loads(await self.complete(c, "fake/seen"))


class StreamingTest(PiClientCase):
    async def test_stream_yields_text_usage_then_done(self):
        events = await self.stream(self.client())
        self.assertEqual([e.kind for e in events], ["text", "text", "text", "usage", "done"])
        self.assertEqual("".join(e.text for e in events if e.kind == "text"), "hello robot")
        usage = events[3].usage
        self.assertEqual((usage["input_tokens"], usage["output_tokens"], usage["cost_usd"], usage["stop_reason"]),
                         (11, 7, 0.001, "end_turn"))

    async def test_complete_and_tool_turn(self):
        c = self.client()
        self.assertEqual(await self.complete(c), "hello robot")
        message = await c.tool_turn(model="fake/tool", system="s", messages=MSG, tools=[{"name": "read_file"}],
                                    max_tokens=100)
        self.assertEqual(message["stop_reason"], "tool_use")
        self.assertEqual(message["content"][1]["input"], {"path": "src/Robot.java"})
        self.assertEqual(message["usage"]["output_tokens"], 7)

    async def test_request_shape_and_default_provider(self):
        c = self.client()
        sent = json.loads(await c.complete(model="request", system="sys", messages=MSG, max_tokens=50, effort="high"))
        self.assertEqual(sent["model"], "anthropic/request")
        self.assertEqual((sent["op"], sent["system"], sent["max_tokens"], sent["effort"]),
                         ("complete", "sys", 50, "high"))
        events = [e async for e in c.stream(model="fake/request", system="s", messages=MSG, max_tokens=5,
                                            system_extra="extra")]
        sent = json.loads("".join(e.text for e in events if e.kind == "text"))
        self.assertEqual((sent["model"], sent["op"], sent["system_extra"]), ("fake/request", "stream", "extra"))
        self.assertNotIn("effort", sent)

    async def test_web_search_is_dropped_with_a_warning(self):
        with self.assertLogs("subzero.pi", "WARNING") as logs:
            events = await self.stream(self.client(), web_searches=3)
        self.assertEqual(events[-1].kind, "done")
        self.assertIn("Web search", logs.output[0])

    async def test_ping_and_models(self):
        c = self.client()
        self.assertEqual((await c.ping())["pi_ai"], "9.9.9")
        self.assertEqual((await c.list_models(["fake"]))[0]["id"], "fake/echo")

    async def test_concurrent_requests_do_not_mix(self):
        c = self.client()
        texts = [f"request number {i} with several words in it" for i in range(4)]
        results = await asyncio.gather(*(self.stream(c, "fake/slow", t) for t in texts))
        for text, events in zip(texts, results):
            self.assertEqual("".join(e.text for e in events if e.kind == "text"), text)
            self.assertEqual(events[-1].kind, "done")


class ProcessTest(PiClientCase):
    async def test_starts_lazily(self):
        c = self.client()
        self.assertIsNone(c._proc)
        await self.complete(c)
        self.assertIsNotNone(c._proc)

    async def test_environment_is_minimal(self):
        with mock.patch.dict(os.environ, {"SECRET_THING": "x", "ANTHROPIC_API_KEY": "sk-parent"}):
            c = self.client(env=child_env({"anthropic": "sk-test-key"}))
            env = (await c.ping())["env"]
        self.assertEqual(env.get("SUBZERO_KEY_ANTHROPIC"), "sk-test-key")
        self.assertNotIn("SECRET_THING", env)
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertTrue(all(k in ("PATH", "HOME", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "SSL_CERT_FILE",
                                  "NODE_EXTRA_CA_CERTS", "TMP", "TEMP", "https_proxy", "http_proxy", "no_proxy",
                                  "SUBZERO_KEY_ANTHROPIC", "LC_CTYPE", "PYTHONIOENCODING") or k.startswith("LC_")
                            for k in env), sorted(env))

    def test_child_env_function(self):
        environ = {"PATH": "/bin", "HTTPS_PROXY": "http://p", "https_proxy": "http://p", "SYSTEMROOT": "C:\\W",
                   "AWS_SECRET": "x", "SUBZERO_KEY_OTHER": "no", "ANTHROPIC_API_KEY": "no"}
        env = child_env({"anthropic": "sk-1", "open-router": "sk-2", "empty": ""}, environ)
        self.assertEqual(env, {"PATH": "/bin", "HTTPS_PROXY": "http://p", "https_proxy": "http://p",
                               "SYSTEMROOT": "C:\\W", "SUBZERO_KEY_ANTHROPIC": "sk-1",
                               "SUBZERO_KEY_OPEN_ROUTER": "sk-2"})
        self.assertEqual(key_env_name("openrouter"), "SUBZERO_KEY_OPENROUTER")

    async def test_aclose_stops_the_process(self):
        c = make_client()
        await self.complete(c)
        proc = c._proc.popen
        await c.aclose()
        self.assertIsNotNone(proc.poll())
        with self.assertRaises(ClaudeError):
            await self.complete(c)

    async def test_crash_fails_in_flight_then_restarts_after_backoff(self):
        c = self.client(backoff_base=0.4)
        pid1 = (await self.seen(c))["pid"]
        events = await self.stream(c, "fake/crash")
        self.assertEqual([e.kind for e in events], ["text", "error"])
        self.assertEqual(events[-1].error, DIED_MESSAGE)
        with self.assertRaisesRegex(ClaudeError, DOWN_MESSAGE):
            await self.complete(c)
        await asyncio.sleep(0.5)
        pid2 = (await self.seen(c))["pid"]
        self.assertNotEqual(pid1, pid2)

    async def test_backoff_doubles_and_resets_after_a_healthy_run(self):
        c = self.client(backoff_base=0.1, healthy_after=0.01)
        await self.stream(c, "fake/crash")
        self.assertAlmostEqual(c._retry_at - time.monotonic(), 0.1, delta=0.1)
        await asyncio.sleep(0.15)
        await self.complete(c)  # restarted, ran past healthy_after before dying below
        await asyncio.sleep(0.05)
        await self.stream(c, "fake/crash")
        self.assertLess(c._retry_at - time.monotonic(), 0.15)  # reset to the base delay, not doubled
        c2 = self.client(backoff_base=0.1, healthy_after=60)
        await self.stream(c2, "fake/crash")
        await asyncio.sleep(0.15)
        await self.stream(c2, "fake/crash")
        self.assertGreater(c2._retry_at - time.monotonic(), 0.1)  # doubled

    async def test_crash_in_complete_raises(self):
        with self.assertRaisesRegex(ClaudeError, "stopped unexpectedly"):
            await self.complete(self.client(), "fake/crash")

    async def test_missing_node_gives_a_plain_error(self):
        c = PiClient(["/no/such/node", "gateway.mjs"], {})
        events = await self.stream(c)
        self.assertEqual([e.kind for e in events], ["error"])
        self.assertIn("Node.js", events[0].error)
        with self.assertRaisesRegex(ClaudeError, "isn't running right now"):
            await self.complete(c)


class FailureTest(PiClientCase):
    async def test_early_close_sends_abort(self):
        c = self.client()
        gen = c.stream(model="fake/loop", system="s", messages=MSG, max_tokens=10)
        first = await gen.__anext__()
        self.assertEqual(first.kind, "text")
        await gen.aclose()
        seen = await self.seen(c)
        self.assertEqual(len(seen["aborted"]), 1)

    async def test_cancellation_sends_abort(self):
        c = self.client()

        async def consume():
            async for _ in c.stream(model="fake/loop", system="s", messages=MSG, max_tokens=10):
                pass

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.3)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(( await self.seen(c))["aborted"]), 1)

    async def test_idle_timeout_aborts_and_fails(self):
        c = self.client(idle_timeout=0.3)
        events = await self.stream(c, "fake/hang")
        self.assertEqual([e.kind for e in events], ["error"])
        self.assertIn("stopped answering", events[0].error)
        self.assertEqual(len((await self.seen(c))["aborted"]), 1)

    async def test_ping_and_models_send_no_abort(self):
        c = self.client()
        await c.ping()
        await c.list_models()
        self.assertEqual((await self.seen(c))["aborted"], [])

    async def test_aborted_ids_are_dropped_when_done_arrives(self):
        c = self.client(idle_timeout=0.2)
        await self.stream(c, "fake/hang")
        proc = c._proc
        for _ in range(50):
            if not proc.aborted:
                break
            await asyncio.sleep(0.05)
        self.assertEqual(proc.aborted, set())

    async def test_gateway_that_ignores_aborts_is_replaced_after_three_timeouts(self):
        c = self.client(idle_timeout=0.2)
        first = (await self.seen(c))["pid"]
        for _ in range(2):
            with self.assertRaisesRegex(ClaudeError, "stopped answering"):
                await self.complete(c, "fake/deaf")
        self.assertIs(c._proc.popen.poll(), None)  # two in a row isn't enough
        with self.assertRaisesRegex(ClaudeError, "stopped answering"):
            await self.complete(c, "fake/deaf")
        self.assertNotEqual((await self.seen(c))["pid"], first)

    async def test_a_finished_request_resets_the_timeout_count(self):
        c = self.client(idle_timeout=0.2)
        first = (await self.seen(c))["pid"]
        for _ in range(2):
            with self.assertRaises(ClaudeError):
                await self.complete(c, "fake/deaf")
        await self.complete(c)
        for _ in range(2):
            with self.assertRaises(ClaudeError):
                await self.complete(c, "fake/deaf")
        self.assertEqual((await self.seen(c))["pid"], first)

    async def test_provider_error_text_is_cut_for_students(self):
        from subzero_ai.pi_client import error_message
        long = "x" * 500
        text = error_message("provider", long, "fake/m")
        self.assertLess(len(text), 260)
        self.assertTrue(text.endswith("…"))
        self.assertIn("boom", error_message("provider", "boom", "fake/m"))

    async def test_total_timeout(self):
        c = self.client(idle_timeout=5, request_timeout=0.4)
        with self.assertRaisesRegex(ClaudeError, "took too long"):
            await self.complete(c, "fake/drip")
        self.assertEqual(len((await self.seen(c))["aborted"]), 1)

    async def test_malformed_lines_are_ignored(self):
        c = self.client()
        with self.assertLogs("subzero.pi", "WARNING") as logs:
            events = await self.stream(c, "fake/garbage")
        self.assertEqual("".join(e.text for e in events if e.kind == "text"), "fine")
        self.assertEqual(events[-1].kind, "done")
        text = "\n".join(logs.output)
        self.assertIn("isn't JSON", text)
        self.assertIn("unknown request zzz", text)
        self.assertEqual(await self.complete(c), "hello robot")  # the reader is still alive

    async def test_error_codes_become_plain_messages(self):
        c = self.client()
        cases = {
            "fake/error": "The AI service returned an error: boom",
            "fake/nokey": "No API key is set for fake. An admin can add it in Settings.",
            "fake/unknown": "The model fake/unknown isn't available.",
            "fake/badreq": "Something went wrong talking to the AI helper.",
        }
        for model, expected in cases.items():
            with self.subTest(model=model):
                events = await self.stream(c, model)
                self.assertEqual([(e.kind, e.error) for e in events], [("error", expected)])
                with self.assertRaises(ClaudeError) as caught:
                    await self.complete(c, model)
                self.assertEqual(str(caught.exception), expected)
        with self.assertRaisesRegex(ClaudeError, "No API key is set for anthropic"):
            await c.complete(model="nokey", system="s", messages=MSG, max_tokens=5)


class FromSettingsTest(unittest.TestCase):
    def test_from_settings(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        s = load_settings(search_cwd=False, data_dir=tmp, anthropic_api_key="sk-a", pi_node="/opt/node",
                          pi_request_timeout=9, pi_idle_timeout=4)
        c = PiClient.from_settings(s)
        self.assertEqual(c.command[0], "/opt/node")
        self.assertTrue(c.command[1].endswith(os.path.join("pi_gateway", "gateway.mjs")))
        self.assertEqual(c.env["SUBZERO_KEY_ANTHROPIC"], "sk-a")
        self.assertEqual((c.request_timeout, c.idle_timeout), (9, 4))
        self.assertIsNone(c._proc)


class AppWiringTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness(ai_backend="pi", pi_node=sys.executable, pi_gateway_script=FAKE, anthropic_api_key="sk-test",
                         model_default="fake/echo", model_titles="fake/echo", model_screener="fake/safe",
                         approval_window_minutes=5)
        self.ava = self.h.login(STUDENT)
        self.kim = self.h.login(MENTOR)

    def tearDown(self):
        self.h.close()

    def test_build_services_makes_a_pi_client_that_does_not_start_yet(self):
        self.assertIsInstance(self.h.services.claude, PiClient)
        self.assertIsNone(self.h.services.claude._proc)

    def test_approval_flow_streams_reply_with_screener_and_title(self):
        body = self.ava.new_chat("How do I tune the arm?").json()
        self.h.idle()
        mid, cid = body["message_id"], int(body["url"].rsplit("/", 1)[1])
        self.assertEqual(self.h.message(mid)["status"], "pending")
        self.assertEqual(self.kim.post(f"/m/{mid}/approve").status_code, 200)
        self.h.idle()
        reply = self.h.reply_to(mid)
        self.assertEqual((reply["status"], reply["content"], reply["model"]),
                         ("complete", "How do I tune the arm?", "fake/echo"))
        self.assertEqual(self.h.services.store.conversation(cid)["title"], "How do I tune the arm")
        # a follow-up inside the window is checked by the screener (fake/safe says SAFE) and goes straight through
        follow = self.ava.send(cid, "And the gains?").json()["message_id"]
        self.h.idle()
        self.assertEqual(self.h.message(follow)["status"], "grace")
        self.assertEqual(self.h.reply_to(follow)["content"], "And the gains?")

    def test_screener_can_flag_through_pi(self):
        self.h.settings.model_screener = "fake/flag"
        verdict = self.h.call(Screener(self.h.settings, self.h.services.claude).check, "hi")
        self.assertFalse(verdict.passed)
        self.assertEqual(verdict.note, "looks odd")

    def test_shutdown_closes_the_client(self):
        client = self.h.services.claude
        asyncio.run(client.ping())  # a throwaway loop; the process is started and then reused below
        proc = client._proc.popen
        self.h.close()
        self.assertIsNotNone(proc.poll())
        self.h = Harness()  # so tearDown has something to close


class NodeResolveTest(unittest.TestCase):
    def test_bundled_node_wins_over_path(self):
        from subzero_ai.pi_client import resolve_node
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        self.assertEqual(resolve_node("", root), "node")
        (root / "node").mkdir()
        (root / "node" / "node.exe").write_bytes(b"")
        self.assertEqual(resolve_node("", root), str(root / "node" / "node.exe"))
        self.assertEqual(resolve_node("/opt/node", root), "/opt/node")

    def test_default_setting_is_empty(self):
        self.assertEqual(Settings().pi_node, "")


class CheckTest(unittest.TestCase):
    def run_check(self, node_runner=lambda node: "v22.23.4", **overrides):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        values = dict(public_url="https://ai.example.org", ai_backend="pi", pi_node=sys.executable,
                      pi_gateway_script=FAKE, anthropic_api_key="sk-x", model_default="fake/echo",
                      model_hard="fake/echo", model_titles="fake/echo", slack_bot_token="xoxb-1",
                      slack_app_token="xapp-1", slack_client_id="1.2", slack_client_secret="s",
                      slack_approvals_channel="CAPPROVALS", admin_slack_ids=frozenset({"U1"}))
        values.update(overrides)
        settings = load_settings(search_cwd=False, data_dir=tmp, **values)
        results = asyncio.run(run_checks(settings, slack=FakeSlack(members={"UBOT"}), node_runner=node_runner))
        return {r.title: r for r in results}

    def test_pi_ok(self):
        results = self.run_check()
        helper = results["The AI helper (Node.js) is running"]
        self.assertEqual(helper.status, OK)
        self.assertIn("pi-ai 9.9.9", helper.detail)
        self.assertEqual(results["Default model answers (fake/echo)"].status, OK)
        self.assertEqual(results["Title model answers (fake/echo)"].status, OK)

    def test_node_version_ok_and_too_old(self):
        ok = self.run_check()["Node.js 22.23.4 is new enough"]
        self.assertEqual(ok.status, OK)
        old = self.run_check(node_runner=lambda node: "v22.18.9")["Node.js is too old"]
        self.assertEqual(old.status, FAIL)
        self.assertIn("22.19", old.detail)
        self.assertEqual(self.run_check(node_runner=lambda node: "v24.0.0")["Node.js 24.0.0 is new enough"].status, OK)

    def test_node_that_will_not_run_fails_plainly(self):
        def boom(node):
            raise FileNotFoundError(node)
        found = self.run_check(node_runner=boom)["Node.js isn't installed or won't start"]
        self.assertEqual(found.status, FAIL)
        self.assertIn("PI_NODE", found.detail)

    def test_pi_node_missing_fails(self):
        found = self.run_check(pi_node="/no/such/node")["The AI helper isn't working"]
        self.assertEqual(found.status, FAIL)
        self.assertIn("Node.js", found.detail)

    def test_pi_model_that_errors_fails(self):
        results = self.run_check(model_hard="fake/error")
        self.assertEqual(results["Hard-problem model didn't answer (fake/error)"].status, FAIL)

    def test_pi_without_anthropic_key_fails_for_claude_models(self):
        results = self.run_check(anthropic_api_key="", model_default="claude-sonnet-5-5")
        self.assertEqual(results["No API key for anthropic"].status, FAIL)
        results = self.run_check(anthropic_api_key="", model_screener="fake/safe")  # all models are fake/*
        self.assertNotIn("No API key for anthropic", results)


if __name__ == "__main__":
    unittest.main()
