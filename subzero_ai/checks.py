"""`python -m subzero_ai check`: confirms config.env, Slack, and the Claude API key work
before anyone depends on them. Each Claude check is a tiny request that costs a fraction of a cent."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from urllib.parse import urlparse

from .claude import ClaudeClient
from .config import Settings
from .slack_api import SlackError, SlackWebApi

OK, WARN, FAIL = "OK", "WARN", "FAIL"


@dataclass
class Check:
    status: str
    title: str
    detail: str = ""


async def run_checks(settings: Settings, *, slack=None, claude=None) -> list[Check]:
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
    if settings.dev_login:
        add(WARN, "Test sign-in is on (DEV_LOGIN=1)",
            "Anyone using this PC can sign in as a sample person. Turn it off before students use the app.")

    # ------------------------------------------------------------------ Claude
    if settings.fake_claude:
        add(WARN, "Using the pretend Claude (FAKE_CLAUDE=1)",
            "Replies are canned. Set ANTHROPIC_API_KEY and FAKE_CLAUDE=0 for real answers.")
    elif not settings.anthropic_api_key:
        add(FAIL, "No Claude API key",
            "Set ANTHROPIC_API_KEY. Create the key inside the SubZero AI workspace at platform.claude.com.")
    else:
        client = claude or ClaudeClient(settings.anthropic_api_key, base_url=settings.anthropic_base_url, max_retries=1)
        models = [("Default model", settings.model_default, settings.effort_default),
                  ("Hard-problem model", settings.model_hard, settings.effort_default),
                  ("Title model", settings.model_titles, None)]
        for label, model, effort in models:
            try:
                await client.complete(model=model, system="Reply with the word OK.",
                                      messages=[{"role": "user", "content": "Connection check"}],
                                      max_tokens=16, effort=effort)
                add(OK, f"{label} answers ({model})")
            except Exception as exc:
                add(FAIL, f"{label} didn't answer ({model})", str(exc))

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


def _slack_reason(exc: Exception) -> str:
    if isinstance(exc, SlackError):
        hints = {
            "invalid_auth": "The token is wrong or was revoked. Copy it again from the Slack app settings.",
            "not_authed": "The token is missing.",
            "account_inactive": "The app was uninstalled. Reinstall it to the workspace.",
            "missing_scope": "The Slack app is missing a permission. Recreate it from slack-manifest.yaml.",
        }
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
