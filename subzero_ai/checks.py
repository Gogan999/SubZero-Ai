"""`python -m subzero_ai check`: confirms config.env, Slack, and the Claude API key work
before anyone depends on them. Each Claude check is a tiny request that costs a fraction of a cent."""

from __future__ import annotations

import asyncio
import functools
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from urllib.parse import urlparse

from .ai_backend import make_client, resolve_backend, unavailable_message
from .config import Settings, model_provider, normalize_model_id
from .pi_client import BRIDGE_PROVIDER, bridge_logged_in, provider_keys, resolve_node
from .sandbox import NETWORK_CHECK_SECONDS, default_run_powershell, describe_network_problem, host_network_problems
from .slack_api import SlackError, SlackWebApi

OK, WARN, FAIL = "OK", "WARN", "FAIL"


@dataclass
class Check:
    status: str
    title: str
    detail: str = ""


async def run_checks(settings: Settings, *, slack=None, claude=None, node_runner=None,
                     sandbox_runner=None) -> list[Check]:
    results: list[Check] = []

    def add(status: str, title: str, detail: str = "") -> None:
        results.append(Check(status, title, detail))

    # ------------------------------------------------------------------ basics
    if settings.config_file:
        add(OK, f"Read settings from {settings.config_file}")
    else:
        add(WARN, "No config.env found", f"Using defaults. Put config.env in {settings.data_dir}.")
    host = urlparse(settings.public_url).hostname or ""
    if settings.https:
        add(OK, f"Public address is {settings.public_url}")
    elif host in ("localhost", "127.0.0.1"):
        add(WARN, "The app is only reachable on this PC",
            "Set PUBLIC_URL to https://ai.yourdomain.org once the Cloudflare Tunnel is running.")
    else:
        add(FAIL, "PUBLIC_URL should start with https://", "Sign in with Slack and secure cookies need HTTPS.")
    if os.name == "nt" and not settings.dev_login:
        problem = data_folder_open(str(settings.data_dir))
        if problem:
            add(WARN, "Other accounts on this PC can open the data folder", problem)
    if settings.dev_login:
        add(WARN, "Test sign-in is on (DEV_LOGIN=1)",
            "Anyone using this PC can sign in as a sample person. Turn it off before students use the app.")

    # ------------------------------------------------------------------ Claude
    backend = resolve_backend(settings)
    if backend == "fake":
        add(WARN, "Using the pretend Claude (FAKE_CLAUDE=1)",
            "Replies are canned (FAKE_CLAUDE=1 or AI_BACKEND=fake). "
            "Set ANTHROPIC_API_KEY, FAKE_CLAUDE=0 and AI_BACKEND=anthropic for real answers.")
    elif backend not in ("anthropic", "pi"):
        add(FAIL, "AI_BACKEND isn't a known backend", unavailable_message(backend))
    else:
        if BRIDGE_PROVIDER in settings.ai_providers:
            if backend != "pi":
                add(FAIL, "The Claude subscription needs AI_BACKEND=pi",
                    "Set AI_BACKEND=pi in config.env, or turn off Claude subscription under AI providers.")
            elif bridge_logged_in(settings.data_dir):
                add(OK, "Claude subscription is signed in (Claude Code login found)")
            else:
                add(FAIL, "Claude subscription isn't signed in",
                    "Sign in under Settings, AI and keys, Claude subscription (or run python -m subzero_ai claude-login).")
        client = claude or make_client(settings, max_retries=1)
        try:
            await _check_models(settings, client, add, pi=backend == "pi", node_runner=node_runner)
        finally:
            if backend == "pi" and claude is None:
                await client.aclose()

    _check_safety_results(settings, add)
    if settings.code_sandbox == "hyperv":
        await _check_sandbox_network(settings, add, sandbox_runner)

    # ------------------------------------------------------------------- Slack
    if slack is None and settings.slack_bot_token:
        slack = SlackWebApi(settings.slack_bot_token)
    if slack is None:
        add(WARN if settings.dev_login else FAIL, "Slack isn't connected",
            "Sign-in, approval cards, and DMs need the Slack bot token. Set SLACK_BOT_TOKEN.")
    else:
        bot_user = None
        try:
            auth = await slack.auth_test()
            bot_user = auth.get("user_id")
            add(OK, f"Slack bot token works (workspace {auth.get('team') or auth.get('team_id')})")
            if settings.slack_team_id and auth.get("team_id") != settings.slack_team_id:
                add(FAIL, "SLACK_TEAM_ID doesn't match the bot token's workspace",
                    "Remove SLACK_TEAM_ID from config.env. The app reads it from the bot token.")
        except Exception as exc:
            add(FAIL, "Slack bot token didn't work", _slack_reason(exc))

        if bot_user is not None:
            channel = settings.slack_approvals_channel
            if not channel:
                add(FAIL, "No approvals channel",
                    "Set SLACK_APPROVALS_CHANNEL to the channel ID of #claude-approvals (starts with C or G).")
            else:
                try:
                    members = set(await slack.channel_members(channel))
                    mentors = members - {bot_user}
                    if mentors:
                        add(OK, f"Approvals channel found: {len(mentors)} mentor{'s' if len(mentors) != 1 else ''}")
                    else:
                        add(WARN, "The approvals channel has no mentors yet", "Add the coaches and mentors to it.")
                except Exception as exc:
                    error = getattr(exc, "error", "")
                    if error in ("not_in_channel", "channel_not_found"):
                        add(FAIL, "The bot can't see the approvals channel",
                            "Check SLACK_APPROVALS_CHANNEL, then type /invite @SubZero AI in #claude-approvals.")
                    else:
                        add(FAIL, "Couldn't read the approvals channel", _slack_reason(exc))

        if not settings.slack_app_token:
            add(FAIL, "No app-level token (SLACK_APP_TOKEN)",
                "Button taps in Slack won't reach the app. Create an app-level token with connections:write.")
        else:
            try:
                await slack.open_socket_url(settings.slack_app_token)
                add(OK, "Slack app-level token works (Socket Mode)")
            except Exception as exc:
                add(FAIL, "Slack app-level token didn't work", _slack_reason(exc))

    if settings.slack_login_enabled:
        add(OK, "Sign in with Slack is set up",
            f"The Slack app must list this redirect URL: {settings.url('/auth/slack/callback')}")
    else:
        add(WARN if settings.dev_login else FAIL, "Sign in with Slack isn't set up",
            "Set SLACK_CLIENT_ID and SLACK_CLIENT_SECRET from the Slack app's Basic Information page.")

    if settings.admin_slack_ids:
        count = len(settings.admin_slack_ids)
        add(OK, f"{count} admin{'s' if count != 1 else ''} named")
    else:
        add(WARN, "No admins named", "Set ADMIN_SLACK_IDS to the coaches' Slack member IDs.")
    return results


def _check_safety_results(settings: Settings, add) -> None:
    """Warn about each model in use that hasn't passed the Model safety check (models set before the check
    existed are allowed to keep working, so this is a warning, not a failure)."""
    from .db import Database
    from .model_check import has_passed
    try:
        db = Database(settings.db_path)
    except Exception:
        return
    roles = [("Default model", settings.model_default, False), ("Hard-problem model", settings.model_hard, False)]
    if settings.screener_enabled:
        roles.append(("Screener model", settings.model_screener, True))
    for label, model, screener in roles:
        model = normalize_model_id(model)
        if not has_passed(db, model, screener=screener):
            add(WARN, f"{label} hasn't passed the model safety check yet ({model})",
                f"Run: python -m subzero_ai check --model {model}")


async def _check_sandbox_network(settings: Settings, add, runner=None) -> None:
    """The host's side of the sandbox network (sandbox.py host_network_check_script). It runs PowerShell, so it is
    only run from the Settings page's background check and the check command, never while a page loads. `runner`
    is injected in tests."""
    if runner is None and os.name != "nt":
        add(WARN, "The sandbox network can't be checked on this computer",
            "It can only be checked on the Windows mini PC, where the Hyper-V sandboxes run.")
        return
    runner = runner or functools.partial(default_run_powershell, timeout=NETWORK_CHECK_SECONDS)
    try:
        problems = await asyncio.wait_for(asyncio.to_thread(
            host_network_problems, settings.sandbox_switch, settings.sandbox_proxy_port, runner),
            NETWORK_CHECK_SECONDS + 5)
    except asyncio.TimeoutError:
        problems = [describe_network_problem("check-failed:the check didn't finish in time")]
    if problems:
        add(FAIL, "The sandbox network isn't locked down, so Code sandboxes won't start",
            "; ".join(problem.rstrip(".") for problem in problems) + ". An administrator needs to run "
            "sandbox-network.ps1 -Action Install once, in an administrator PowerShell in the SubZero AI folder.")
    else:
        add(OK, "The sandbox network is locked down",
            f"Sandboxes can reach only this PC, on the switch {settings.sandbox_switch}.")


async def _check_models(settings: Settings, client, add, *, pi: bool, node_runner=None) -> None:
    """Probe the default, hard-problem, title and screener models with a tiny request each."""
    models = [("Default model", settings.model_default, settings.effort_default),
              ("Hard-problem model", settings.model_hard, settings.effort_default),
              ("Title model", settings.model_titles, None),
              ("Screener model", settings.model_screener, None)]
    models = [(label, normalize_model_id(model), effort) for label, model, effort in models]
    if pi:
        check_node_version(resolve_node(settings.pi_node), add, node_runner)
        try:
            pong = await client.ping()
            add(OK, "The AI helper (Node.js) is running",
                f"Node {pong.get('node', '?')}, pi-ai {pong.get('pi_ai', '?')}")
        except Exception as exc:
            add(FAIL, "The AI helper isn't working", f"{exc} Check PI_NODE in config.env and the log.")
            return
        keys = provider_keys(settings)
        used = dict.fromkeys(model_provider(model) for _, model, _ in models)
        missing = [p for p in used if p in settings.ai_providers and p not in keys and p != BRIDGE_PROVIDER]
        for provider in missing:
            add(FAIL, f"No API key for {provider}", "An admin can add it on the Settings page, under AI providers.")
        models = [m for m in models if model_provider(m[1]) not in missing]
    elif not settings.anthropic_api_key:
        add(FAIL, "No Claude API key",
            "Set ANTHROPIC_API_KEY. Create the key inside the SubZero AI workspace at platform.claude.com.")
        return
    for label, model, effort in models:
        try:
            await client.complete(model=model, system="Reply with the word OK.",
                                  messages=[{"role": "user", "content": "Connection check"}],
                                  max_tokens=16, effort=effort)
            add(OK, f"{label} answers ({model})")
        except Exception as exc:
            add(FAIL, f"{label} didn't answer ({model})", str(exc))


MIN_NODE = (22, 19)


def node_version_output(node: str) -> str:
    """What `<node> --version` prints, e.g. "v22.23.4". Raises OSError or subprocess errors if it can't run."""
    done = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=20)
    return done.stdout.strip()


def check_node_version(node: str, add, runner=None) -> None:
    """OK if Node.js runs and is 22.19 or newer, otherwise FAIL with a plain message. `runner(node)` returns the
    version text (injected in tests)."""
    need = ".".join(str(n) for n in MIN_NODE)
    try:
        text = (runner or node_version_output)(node)
    except Exception:
        add(FAIL, "Node.js isn't installed or won't start",
            f"The AI helper needs Node.js {need} or newer. Tried: {node}. Use the Windows release, which includes it, "
            "or set PI_NODE in config.env to the full path of node.exe.")
        return
    found = re.match(r"v?(\d+)\.(\d+)", text or "")
    if found and (int(found.group(1)), int(found.group(2))) >= MIN_NODE:
        add(OK, f"Node.js {text.lstrip('v')} is new enough", f"The AI helper needs {need} or newer. Using {node}.")
    else:
        add(FAIL, "Node.js is too old",
            f"Found {text or 'no version'} at {node}, but the AI helper needs {need} or newer. "
            "Use the Windows release, which includes it, or set PI_NODE in config.env to a newer node.exe.")


def data_folder_open(path: str, icacls_output: str | None = None) -> str:
    """On Windows, the data folder (with config.env and the chats) should be open only to administrators,
    SYSTEM, and the app's service account. Returns what's wrong, or an empty string."""
    if icacls_output is None:
        try:
            icacls_output = subprocess.run(["icacls", path], capture_output=True, text=True, timeout=15).stdout
        except (OSError, subprocess.SubprocessError):
            return ""
    for group in ("BUILTIN\\Users", "Everyone", "NT AUTHORITY\\Authenticated Users", "CREATOR OWNER"):
        if group.lower() in icacls_output.lower():
            return (f"{group} has access to {path}. Run install.cmd again (it locks the folder down), "
                    "and keep config.env private.")
    return ""


def _slack_reason(exc: Exception) -> str:
    if isinstance(exc, SlackError):
        hints = {
            "invalid_auth": "The token is wrong or was revoked. Copy it again from the Slack app settings.",
            "not_authed": "The token is missing.",
            "account_inactive": "The app was uninstalled. Reinstall it to the workspace.",
        }
        if exc.error == "missing_scope":
            scope = f" ({exc.needed})" if exc.needed else ""
            return (f"The Slack app is missing a permission{scope}. Add it under OAuth & Permissions "
                    "(see README.md, \"Slack permissions\"), then reinstall the app and copy the new bot token.")
        return hints.get(exc.error, f"Slack said: {exc.error}")
    return str(exc)


def print_report(results: list[Check], out=None) -> int:
    out = out or sys.stdout
    print("SubZero AI settings check", file=out)
    print("", file=out)
    for r in results:
        print(f"  {r.status:<5} {r.title}", file=out)
        if r.detail:
            print(f"        {r.detail}", file=out)
    fails = sum(r.status == FAIL for r in results)
    warns = sum(r.status == WARN for r in results)
    print("", file=out)
    if fails:
        print(f"{fails} problem{'s' if fails != 1 else ''} to fix, {warns} warning{'s' if warns != 1 else ''}. "
              "Fix config.env, then run the check again.", file=out)
    else:
        print(f"Everything needed is working ({warns} warning{'s' if warns != 1 else ''}).", file=out)
    return 1 if fails else 0
