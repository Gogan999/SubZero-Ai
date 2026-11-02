"""Provider/model ids: the migration, setting validation, the Anthropic client's prefix handling, API key saving,
and PiClient picking up new keys."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from subzero_ai import model_check, saved_settings
from subzero_ai.ai_backend import normalize_model_id
from subzero_ai.claude import ClaudeClient, ClaudeError, FakeClaude
from subzero_ai.config import Settings, load_settings
from subzero_ai.db import MIGRATIONS, Database
from subzero_ai.pi_client import PiClient, child_env, provider_keys
from subzero_ai.web import model_label

from .helpers import ADMIN, MENTOR, STUDENT, Harness, pass_model_check

FAKE = str(Path(__file__).with_name("fake_pi_gateway.py"))
HTML = {"Accept": "text/html"}
KEY = "sk-ant-SECRETSECRETSECRET1234"
OPENAI_KEY = "sk-openaiSECRETSECRET123456"


class MigrationTest(unittest.TestCase):
    def test_migration_11_rewrites_bare_claude_ids(self):
        tmp = Path(tempfile.mkdtemp(prefix="subzero-mig-"))
        path = tmp / "v10.db"
        conn = sqlite3.connect(path)
        conn.executescript("BEGIN;" + "".join(MIGRATIONS[:10]) + "PRAGMA user_version = 10; COMMIT;")
        conn.execute("INSERT INTO users (slack_user_id, name, created_at) VALUES ('U1', 'Ava', 'now')")
        for model in ("claude-sonnet-5-5", "anthropic/claude-opus-5-5", "openrouter/meta-llama/llama-4"):
            conn.execute("INSERT INTO conversations (owner_id, model, created_at, updated_at) VALUES (1, ?, 'n', 'n')",
                         (model,))
        for model in ("claude-haiku-5-5", None, "fake/echo"):
            conn.execute("INSERT INTO messages (conversation_id, author_id, role, content, status, model, created_at, "
                         "updated_at) VALUES (1, 1, 'assistant', 'hi', 'complete', ?, 'n', 'n')", (model,))
        for model in ("claude-opus-5-5", "openai/gpt-x"):
            conn.execute("INSERT INTO code_sessions (title, task, status, model, repo, created_by, created_at, "
                         "updated_at) VALUES ('t', 't', 'done', ?, 'o/r', 1, 'n', 'n')", (model,))
        for key, value in (("model_default", "claude-sonnet-5-5"), ("model_hard", "claude-opus-5-5"),
                           ("model_titles", "claude-haiku-5-5"), ("model_screener", "openai/gpt-x"),
                           ("subteams", "claude-club")):
            conn.execute("INSERT INTO settings (key, value, updated_at) VALUES (?, ?, 'n')", (key, value))
        conn.commit()
        conn.close()

        db = Database(path)
        self.assertEqual(db.one("PRAGMA user_version")[0], len(MIGRATIONS))
        self.assertGreaterEqual(len(MIGRATIONS), 11)
        column = lambda sql: [r[0] for r in db.all(sql)]  # noqa: E731
        self.assertEqual(column("SELECT model FROM conversations ORDER BY id"),
                         ["anthropic/claude-sonnet-5-5", "anthropic/claude-opus-5-5", "openrouter/meta-llama/llama-4"])
        self.assertEqual(column("SELECT model FROM messages ORDER BY id"),
                         ["anthropic/claude-haiku-5-5", None, "fake/echo"])
        self.assertEqual(column("SELECT model FROM code_sessions ORDER BY id"),
                         ["anthropic/claude-opus-5-5", "openai/gpt-x"])
        saved = {r["key"]: r["value"] for r in db.all("SELECT key, value FROM settings")}
        self.assertEqual(saved, {"model_default": "anthropic/claude-sonnet-5-5",
                                 "model_hard": "anthropic/claude-opus-5-5",
                                 "model_titles": "anthropic/claude-haiku-5-5", "model_screener": "openai/gpt-x",
                                 "subteams": "claude-club"})


class NormalizeTest(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_model_id(" claude-sonnet-5-5 "), "anthropic/claude-sonnet-5-5")
        self.assertEqual(normalize_model_id("anthropic/claude-sonnet-5-5"), "anthropic/claude-sonnet-5-5")
        self.assertEqual(normalize_model_id("openai/gpt-x"), "openai/gpt-x")
        self.assertEqual(normalize_model_id(None), "")

    def test_config_env_values_are_normalized(self):
        tmp = Path(tempfile.mkdtemp(prefix="subzero-cfg-"))
        (tmp / "config.env").write_text("MODEL_DEFAULT=claude-opus-5-5\nMODEL_SCREENER=openai/gpt-x\n"
                                        "AI_PROVIDERS=openai, bogus\n", encoding="utf-8")
        settings = load_settings(search_cwd=False, data_dir=tmp)
        self.assertEqual(settings.model_default, "anthropic/claude-opus-5-5")
        self.assertEqual(settings.model_screener, "openai/gpt-x")
        self.assertEqual(settings.model_hard, "anthropic/claude-opus-5-5")
        self.assertEqual(settings.ai_providers, ("openai",))

    def test_labels(self):
        self.assertEqual(model_label("claude-sonnet-5-5"), "Sonnet 5.5")
        self.assertEqual(model_label("anthropic/claude-opus-5-5"), "Opus 5.5")
        self.assertEqual(model_label("openai/gpt-x"), "gpt-x")
        self.assertEqual(model_label("openai/gpt-x", {"openai/gpt-x": "GPT X"}), "GPT X")

    def test_settings_repr_hides_keys(self):
        s = Settings(anthropic_api_key=KEY, openai_api_key=OPENAI_KEY, gemini_api_key="gem-SECRET",
                     openrouter_api_key="or-SECRET", slack_bot_token="xoxb-SECRET", slack_app_token="xapp-SECRET",
                     slack_client_secret="slack-SECRET", connector_github_token="ghp_SECRET")
        text = repr(s)
        for secret in ("SECRET", KEY, OPENAI_KEY):
            self.assertNotIn(secret, text)


class ValidationTest(unittest.TestCase):
    def settings(self, **kw) -> Settings:
        values = dict(ai_backend="pi", ai_providers=("anthropic", "openrouter"))
        values.update(kw)
        return Settings(**values)

    def check(self, model, **kw):
        values, errors = saved_settings.validate("window", {"model_screener": model, "approval_window_minutes": "5"},
                                                 settings=self.settings(**kw))
        return values.get("model_screener"), errors.get("model_screener")

    def test_allowed(self):
        self.assertEqual(self.check("anthropic/claude-sonnet-5-5"), ("anthropic/claude-sonnet-5-5", None))
        self.assertEqual(self.check("claude-haiku-5-5"), ("anthropic/claude-haiku-5-5", None))
        self.assertEqual(self.check("openrouter/meta-llama/llama-4"), ("openrouter/meta-llama/llama-4", None))
        self.assertEqual(self.check("openrouter/vendor/model:free"), ("openrouter/vendor/model:free", None))

    def test_refused(self):
        self.assertIn("provider/model", self.check("gpt-4")[1])
        self.assertIn("provider/model", self.check("")[1])
        self.assertIn("provider/model", self.check("anthropic/bad id")[1])
        self.assertIn("provider/model", self.check("anthropic/" + "a" * 101)[1])
        self.assertIn("provider/model", self.check("anthropic/x;rm")[1])
        self.assertIn("isn't a provider we support", self.check("mistral/large")[1])
        self.assertIn("isn't turned on", self.check("openai/gpt-x")[1])
        self.assertIn("AI_BACKEND=anthropic", self.check("openrouter/meta-llama/llama-4", ai_backend="anthropic")[1])

    def test_fable_and_mythos_are_blocked_for_every_provider(self):
        for model in ("anthropic/claude-fable-5-1", "claude-mythos-1", "openrouter/anthropic/claude-fable-5-1",
                      "openrouter/some/Mythos-large"):
            self.assertIn("kept off", self.check(model)[1], model)

    def test_fake_backend_takes_any_enabled_provider(self):
        self.assertEqual(self.check("openrouter/a/b", ai_backend="fake")[1], None)

    def providers(self, form, **kw):
        base = {"model_default": "anthropic/claude-sonnet-5-5", "model_hard": "anthropic/claude-opus-5-5",
                "model_titles": "anthropic/claude-haiku-5-5", "effort_default": "medium", "effort_high": "high",
                "max_tokens_chat": "8000", "max_tokens_explainer": "16000", "long_chat_tokens": "150000",
                "web_search_daily_limit": "50"}
        base.update(form)
        return saved_settings.validate("claude", base, settings=self.settings(**kw))

    def test_provider_list(self):
        values, errors = self.providers({"ai_providers": "openai\nanthropic"})
        self.assertEqual((values["ai_providers"], errors), (("anthropic", "openai"), {}))
        self.assertIn("isn't a provider", self.providers({"ai_providers": "anthropic\nbogus"})[1]["ai_providers"])
        self.assertIn("at least one", self.providers({"ai_providers": ""})[1]["ai_providers"])

    def test_anthropic_cant_be_removed_while_a_model_uses_it(self):
        _, errors = self.providers({"ai_providers": "openrouter"})
        self.assertIn("is still used", errors["ai_providers"])
        # fine once the models move; the screener (another section) still uses anthropic, so it still blocks
        moved = {"ai_providers": "openrouter", "model_default": "openrouter/a/b", "model_hard": "openrouter/a/c",
                 "model_titles": "openrouter/a/d"}
        self.assertIn("screener model", self.providers(moved)[1]["ai_providers"])
        values, errors = self.providers(moved, model_screener="openrouter/a/e")
        self.assertEqual(errors, {})
        self.assertEqual(values["ai_providers"], ("openrouter",))


class ClaudeClientTest(unittest.TestCase):
    def test_prefix_is_stripped_and_other_providers_refused(self):
        body = ClaudeClient.request_body(model="anthropic/claude-sonnet-5-5", system="s", messages=[], max_tokens=5)
        self.assertEqual(body["model"], "claude-sonnet-5-5")
        body = ClaudeClient.request_body(model="claude-sonnet-5-5", system="s", messages=[], max_tokens=5)
        self.assertEqual(body["model"], "claude-sonnet-5-5")
        with self.assertRaises(ClaudeError) as caught:
            ClaudeClient.request_body(model="openai/gpt-x", system="s", messages=[], max_tokens=5)
        self.assertEqual(str(caught.exception), "This chat's model (openai/gpt-x) needs AI_BACKEND=pi.")

    def test_stream_complete_and_tool_turn_refuse_other_providers(self):
        client = ClaudeClient("sk-ant-x")

        async def run():
            events = [e async for e in client.stream(model="openai/gpt-x", system="s", messages=[], max_tokens=5)]
            self.assertEqual([e.kind for e in events], ["error"])
            self.assertIn("needs AI_BACKEND=pi", events[0].error)
            with self.assertRaises(ClaudeError):
                await client.complete(model="openai/gpt-x", system="s", messages=[], max_tokens=5)
            with self.assertRaises(ClaudeError):
                await client.tool_turn(model="openai/gpt-x", system="s", messages=[], tools=[], max_tokens=5)

        asyncio.run(run())

    def test_set_keys(self):
        client = ClaudeClient("sk-ant-old")
        client.set_keys({"openai": "x"})
        self.assertEqual(client.api_key, "sk-ant-old")
        client.set_keys({"anthropic": "sk-ant-new"})
        self.assertEqual(client.api_key, "sk-ant-new")


class ProviderKeysTest(unittest.TestCase):
    def test_only_set_keys_of_enabled_providers(self):
        s = Settings(anthropic_api_key="a", openai_api_key="o", gemini_api_key="g", openrouter_api_key="r",
                     ai_providers=("openai", "google"))
        self.assertEqual(provider_keys(s), {"anthropic": "a", "openai": "o", "google": "g"})
        s = Settings(openai_api_key="o", ai_providers=("anthropic",))
        self.assertEqual(provider_keys(s), {})


class RecordingClaude(FakeClaude):
    def __init__(self):
        super().__init__(delay=0)
        self.keys: list[dict] = []

    def set_keys(self, keys):
        self.keys.append(dict(keys))


class KeyRouteTest(unittest.TestCase):
    def setUp(self):
        self.claude = RecordingClaude()
        self.h = Harness(claude=self.claude, ai_backend="fake", ai_providers=("anthropic", "openai"))
        self.admin = self.h.login(ADMIN)

    def tearDown(self):
        self.h.close()

    def config(self) -> str:
        path = self.h.settings.config_path
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def test_admin_only_and_csrf(self):
        for who in (STUDENT, MENTOR):
            self.assertEqual(self.h.login(who).post("/settings/ai-keys", {"anthropic_api_key": KEY}).status_code, 403)
        self.assertEqual(self.admin.post("/settings/ai-keys", {"anthropic_api_key": KEY}, csrf=False).status_code, 403)
        self.assertEqual(self.config(), "")
        self.assertEqual(self.claude.keys, [])

    def test_saves_without_echoing_and_updates_the_client(self):
        resp = self.admin.post("/settings/ai-keys", {"anthropic_api_key": KEY, "openai_api_key": OPENAI_KEY,
                                                     "openrouter_api_key": ""}, headers=HTML,
                               follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        for secret in (KEY, OPENAI_KEY):
            self.assertNotIn(secret, resp.text)
            self.assertNotIn(secret, resp.headers.get("location", ""))
        self.assertIn("ANTHROPIC_API_KEY=" + KEY, self.config())
        self.assertIn("OPENAI_API_KEY=" + OPENAI_KEY, self.config())
        self.assertNotIn("OPENROUTER", self.config())
        self.assertEqual(self.h.settings.anthropic_api_key, KEY)
        self.assertEqual(self.claude.keys, [{"anthropic": KEY, "openai": OPENAI_KEY}])
        row = self.h.services.db.one("SELECT details FROM audit_log WHERE action = 'ai_key_saved'")
        self.assertEqual(json.loads(row["details"]), {"providers": ["anthropic", "openai"]})
        for line in self.h.services.db.all("SELECT details FROM audit_log"):
            self.assertNotIn("SECRET", line["details"] or "")
        page = self.admin.get("/settings").text
        self.assertIn("Saved", page)
        self.assertNotIn(KEY, page)
        self.assertNotIn(OPENAI_KEY, page)

    def test_empty_boxes_keep_saved_keys_and_pages_show_status(self):
        page = self.admin.get("/settings").text
        self.assertIn("Not set", page)
        self.assertIn('name="openai_api_key"', page)
        self.assertNotIn('name="openrouter_api_key"', page)  # not turned on
        self.admin.post("/settings/ai-keys", {"anthropic_api_key": KEY})
        self.admin.post("/settings/ai-keys", {"anthropic_api_key": "", "openai_api_key": OPENAI_KEY})
        self.assertIn("ANTHROPIC_API_KEY=" + KEY, self.config())
        self.assertEqual(self.claude.keys[-1], {"anthropic": KEY, "openai": OPENAI_KEY})

    def test_bad_keys_are_refused(self):
        for form in ({"anthropic_api_key": "sk-wrong-key-with-no-ant-prefix"}, {"anthropic_api_key": "sk-ant-x"},
                     {"anthropic_api_key": "sk-ant-has a space in it 123456"}, {"openai_api_key": "short"},
                     {"openrouter_api_key": OPENAI_KEY},  # openrouter isn't turned on
                     {"openai_api_key": "x" * 600}, {}):
            resp = self.admin.post("/settings/ai-keys", form)
            self.assertIn(resp.status_code, (200, 303), form)
            self.assertEqual(self.config(), "", form)
        self.assertEqual(self.claude.keys, [])
        self.assertIsNone(self.h.services.db.one("SELECT 1 FROM audit_log WHERE action = 'ai_key_saved'"))

    def test_model_form_saves_providers(self):
        form = {"ai_providers": ["anthropic", "openai"], "model_default": "claude-sonnet-5-5",
                "model_hard": "openai/gpt-x", "model_titles": "claude-haiku-5-5", "effort_default": "medium",
                "effort_high": "high", "max_tokens_chat": "8000", "max_tokens_explainer": "16000",
                "long_chat_tokens": "150000", "web_search_daily_limit": "50"}
        # the offline backend takes any enabled provider, and a new hard-problem model needs a passing safety check
        model_check.save_result(self.h.services.db, model_check.CheckResult("openai/gpt-x", cases=[
            model_check.CaseResult(c, True) for c in model_check.CHAT_CASES + model_check.SCREENER_CASES]))
        resp = self.admin.post("/settings/claude", form, headers=HTML)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn("Saved Claude settings.", resp.text)
        self.assertEqual(self.h.settings.model_hard, "openai/gpt-x")
        self.assertEqual(self.h.settings.model_default, "anthropic/claude-sonnet-5-5")


class SetupKeyStepTest(unittest.TestCase):
    def test_setup_saves_key_into_the_running_client(self):
        claude = RecordingClaude()
        h = Harness(claude=claude)
        try:
            h.services.claude_factory = lambda key: FakeClaude(delay=0)
            resp = h.client.post("/setup/claude", data={"anthropic_api_key": KEY},
                                 headers={"X-CSRF-Token": self.csrf(h)})
            self.assertEqual(resp.status_code, 200, resp.text)
            self.assertEqual(claude.keys, [{"anthropic": KEY}])
        finally:
            h.close()

    @staticmethod
    def csrf(h) -> str:
        page = h.client.get("/setup")
        from .helpers import CSRF_RE
        return CSRF_RE.search(page.text).group(1)


class MentorSwitchTest(unittest.TestCase):
    def test_mentors_pick_from_checked_models_only(self):
        h = Harness(model_default="claude-sonnet-5-5", model_hard="anthropic/claude-opus-5-5",
                    anthropic_api_key="sk-ant-test-key-0123456789")
        try:
            ava, kim = h.login(STUDENT), h.login(MENTOR)
            pass_model_check(h, "anthropic/claude-sonnet-5-5")
            pass_model_check(h, "anthropic/claude-opus-5-5")
            cid = int(ava.new_chat("Hello robot").json()["url"].rsplit("/", 1)[1])
            page = kim.get(f"/c/{cid}", headers=HTML).text
            self.assertIn('value="anthropic/claude-sonnet-5-5"', page)
            self.assertIn('value="anthropic/claude-opus-5-5"', page)
            self.assertNotIn("haiku", page)  # not checked, so not offered
            self.assertEqual(kim.post(f"/c/{cid}/model", {"model": "anthropic/claude-haiku-5-5"}).status_code, 400)
            self.assertEqual(kim.post(f"/c/{cid}/model", {"model": "claude-opus-5-5"}).status_code, 200)
            self.assertEqual(h.services.store.conversation(cid)["model"], "anthropic/claude-opus-5-5")
        finally:
            h.close()


class PiKeysCase(unittest.IsolatedAsyncioTestCase):
    def client(self, keys=None) -> PiClient:
        c = PiClient([sys.executable, FAKE], child_env(keys or {"anthropic": "sk-old"}), kill_wait=3)
        self.addAsyncCleanup(c.aclose)
        return c

    async def env(self, c: PiClient) -> dict:
        return (await c.ping())["env"]

    async def test_idle_gateway_restarts_with_new_keys(self):
        c = self.client()
        before = await self.env(c)
        self.assertEqual(before["SUBZERO_KEY_ANTHROPIC"], "sk-old")
        self.assertNotIn("SUBZERO_KEY_OPENAI", before)
        old = c._proc
        c.set_keys({"anthropic": "sk-new", "openai": "sk-oa"})
        after = await self.env(c)
        self.assertEqual((after["SUBZERO_KEY_ANTHROPIC"], after["SUBZERO_KEY_OPENAI"]), ("sk-new", "sk-oa"))
        self.assertIsNot(c._proc, old)
        for _ in range(100):
            if old.popen.poll() is not None:
                break
            await asyncio.sleep(0.05)
        self.assertIsNotNone(old.popen.poll(), "the old gateway should have stopped")

    async def test_keys_removed_from_the_set_are_dropped(self):
        c = self.client({"anthropic": "sk-old", "openai": "sk-oa"})
        await self.env(c)
        c.set_keys({"anthropic": "sk-old"})
        self.assertNotIn("SUBZERO_KEY_OPENAI", await self.env(c))

    async def test_set_keys_before_any_request_just_changes_the_env(self):
        c = self.client()
        c.set_keys({"anthropic": "sk-new"})
        self.assertEqual((await self.env(c))["SUBZERO_KEY_ANTHROPIC"], "sk-new")

    async def test_in_flight_request_is_not_cut_off(self):
        c = self.client()
        text = "one two three four five six seven eight nine ten"
        stream = c.stream(model="fake/slow", system="s", messages=[{"role": "user", "content": text}], max_tokens=50)
        first = await stream.__anext__()
        self.assertEqual(first.kind, "text")
        old = c._proc
        c.set_keys({"anthropic": "sk-new"})
        self.assertIsNone(c._proc)
        self.assertIsNone(old.popen.poll(), "the busy gateway keeps running")
        # a new request meanwhile goes to a fresh gateway with the new key
        self.assertEqual((await self.env(c))["SUBZERO_KEY_ANTHROPIC"], "sk-new")
        events = [first] + [e async for e in stream]
        self.assertEqual("".join(e.text for e in events if e.kind == "text"), text)
        self.assertEqual(events[-1].kind, "done")
        for _ in range(100):
            if old.popen.poll() is not None:
                break
            await asyncio.sleep(0.05)
        self.assertIsNotNone(old.popen.poll(), "the old gateway stops after its last request")


if __name__ == "__main__":
    unittest.main()


class EmptyProvidersTest(unittest.TestCase):
    def test_unticking_every_provider_is_refused(self):
        h = Harness(ai_backend="fake")
        try:
            admin = h.login(ADMIN)
            page = admin.get("/settings").text
            self.assertIn('<input type="hidden" name="ai_providers" value="">', page)
            form = {"ai_providers": "", "model_default": "claude-sonnet-5-5", "model_hard": "claude-opus-5-5",
                    "model_titles": "claude-haiku-5-5", "effort_default": "medium", "effort_high": "high",
                    "max_tokens_chat": "8000", "max_tokens_explainer": "16000", "long_chat_tokens": "150000",
                    "web_search_daily_limit": "50"}
            resp = admin.post("/settings/claude", form, headers=HTML)
            self.assertEqual(resp.status_code, 400)
            self.assertIn("Turn on at least one provider.", resp.text)
            # the hidden empty value doesn't count when boxes are ticked
            resp = admin.post("/settings/claude", {**form, "ai_providers": ["", "anthropic", "openai"]}, headers=HTML)
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(h.settings.ai_providers, ("anthropic", "openai"))
        finally:
            h.close()


class PiRaceCase(unittest.IsolatedAsyncioTestCase):
    async def test_request_never_lands_on_a_retired_process(self):
        c = PiClient([sys.executable, FAKE], child_env({"anthropic": "sk-old"}))
        self.addAsyncCleanup(c.aclose)
        await c.ping()
        old = c._proc
        real = c._ensure_proc
        calls = []

        def racing():
            proc = real()
            if not calls:  # the first pick is retired right after being chosen
                calls.append(proc)
                c.set_keys({"anthropic": "sk-new"})
            return proc

        c._ensure_proc = racing
        pong = await c.ping()
        self.assertEqual(pong["env"]["SUBZERO_KEY_ANTHROPIC"], "sk-new")
        self.assertIsNot(c._proc, old)
