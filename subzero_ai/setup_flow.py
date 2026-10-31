"""The first-run setup page's logic: which steps are done, the Create Slack app link, and the recovery code.

Setup answers only on the mini PC itself and locks once it's finished. The recovery code lets an admin sign in
on the mini PC if Slack sign-in ever breaks.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from urllib.parse import quote, urlparse

from .config import Settings
from .db import Database, utcnow

RECOVERY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I, which are easy to misread
RECOVERY_ITERATIONS = 200_000
PLACEHOLDER_HOSTS = ("yourdomain.org", "example.org", "example.com")


# --------------------------------------------------------------------------- state
def setup_complete(db: Database) -> bool:
    return db.get_kv("setup_complete") is not None


def mark_setup_complete(db: Database, actor: str = "setup page") -> None:
    db.set_kv("setup_complete", utcnow())
    db.audit("setup_finished", actor_id=None, details={"by": actor})


def reopen_setup(db: Database) -> None:
    db.delete_kv("setup_complete")
    db.audit("setup_reopened", actor_id=None, details={"by": "command line on the mini PC"})


def address_ok(public_url: str) -> bool:
    parsed = urlparse(public_url)
    host = parsed.hostname or ""
    return (parsed.scheme == "https" and bool(host) and "." in host and parsed.path in ("", "/")
            and not any(host.endswith(p) for p in PLACEHOLDER_HOSTS))


def steps(settings: Settings) -> dict[str, bool]:
    """Which setup steps are done, in order."""
    return {
        "address": address_ok(settings.public_url),
        "slack": all([settings.slack_bot_token, settings.slack_app_token, settings.slack_client_id,
                      settings.slack_client_secret]),
        "channel": bool(settings.slack_approvals_channel),
        "claude": bool(settings.anthropic_api_key),
        "admins": bool(settings.admin_slack_ids),
    }


def config_is_complete(settings: Settings) -> bool:
    return all(steps(settings).values())


# --------------------------------------------------------------------------- Slack app manifest
def slack_manifest(settings: Settings, public_url: str | None = None) -> dict:
    """The same app as slack-manifest.yaml, with this site's sign-in address filled in."""
    base = (public_url or settings.public_url).rstrip("/")
    return {
        "display_information": {
            "name": settings.app_name,
            "description": "Mentor-approved Claude for the robotics team",
            "background_color": "#1a5a86",
        },
        "features": {
            "app_home": {"home_tab_enabled": False, "messages_tab_enabled": True,
                         "messages_tab_read_only_enabled": False},
            "bot_user": {"display_name": settings.app_name, "always_online": True},
        },
        "oauth_config": {
            "redirect_urls": [base + "/auth/slack/callback"],
            "scopes": {
                "bot": ["chat:write", "im:write", "im:history", "groups:read", "users:read"],
                "user": ["openid", "email", "profile"],
            },
        },
        "settings": {
            "event_subscriptions": {"bot_events": ["message.im"]},
            "interactivity": {"is_enabled": True},
            "org_deploy_enabled": False,
            "socket_mode_enabled": True,
            "token_rotation_enabled": False,
        },
    }


def create_app_link(manifest: dict) -> str:
    """Slack's Create app page with the manifest filled in."""
    return ("https://api.slack.com/apps?new_app=1&manifest_json="
            + quote(json.dumps(manifest, separators=(",", ":")), safe=""))


# --------------------------------------------------------------------------- recovery code
def new_recovery_code(db: Database) -> str:
    """Make a new recovery code, keep only its hash, and return it to show once."""
    raw = "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(20))
    code = "-".join(raw[i:i + 5] for i in range(0, 20, 5))
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", _normalize(code).encode(), salt, RECOVERY_ITERATIONS)
    db.set_kv("recovery_hash", f"pbkdf2_sha256${RECOVERY_ITERATIONS}${salt.hex()}${digest.hex()}")
    db.audit("recovery_code_created", actor_id=None)
    return code


def check_recovery_code(db: Database, code: str) -> bool:
    stored = db.get_kv("recovery_hash")
    if not stored or not code:
        return False
    try:
        _, iterations, salt, expected = stored.split("$")
        digest = hashlib.pbkdf2_hmac("sha256", _normalize(code).encode(), bytes.fromhex(salt), int(iterations))
    except ValueError:
        return False
    return hmac.compare_digest(digest.hex(), expected)


def _normalize(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch.isalnum())
