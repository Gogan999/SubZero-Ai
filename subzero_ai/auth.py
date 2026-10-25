"""Sign in with Slack (OpenID Connect)."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

import anyio
import jwt
import requests

from .config import Settings

log = logging.getLogger("subzero.auth")

AUTHORIZE_URL = "https://slack.com/openid/connect/authorize"
TOKEN_URL = "https://slack.com/api/openid.connect.token"
JWKS_URL = "https://slack.com/openid/connect/keys"
ISSUER = "https://slack.com"
TEAM_CLAIM = "https://slack.com/team_id"
USER_CLAIM = "https://slack.com/user_id"


class LoginError(Exception):
    """Shown to the person signing in, so the text is written for them."""


class SlackLogin:
    def __init__(self, settings: Settings, *, session: requests.Session | None = None,
                 fetch_jwks: Callable[[], dict[str, Any]] | None = None):
        self.settings = settings
        self.session = session or requests.Session()
        self._fetch_jwks = fetch_jwks or self._download_jwks
        self._jwks: dict[str, Any] | None = None
        self._jwks_at = 0.0

    @property
    def redirect_uri(self) -> str:
        return self.settings.url("/auth/slack/callback")

    def authorize_url(self, state: str, nonce: str) -> str:
        params = {
            "response_type": "code",
            "scope": "openid profile email",
            "client_id": self.settings.slack_client_id,
            "state": state,
            "nonce": nonce,
            "redirect_uri": self.redirect_uri,
        }
        if self.settings.slack_team_id:
            params["team"] = self.settings.slack_team_id
        return AUTHORIZE_URL + "?" + urlencode(params)

    async def finish(self, code: str, nonce: str) -> dict[str, Any]:
        return await anyio.to_thread.run_sync(lambda: self._finish_sync(code, nonce))

    def _finish_sync(self, code: str, nonce: str) -> dict[str, Any]:
        try:
            resp = self.session.post(TOKEN_URL, data={
                "code": code,
                "client_id": self.settings.slack_client_id,
                "client_secret": self.settings.slack_client_secret,
                "redirect_uri": self.redirect_uri,
            }, timeout=15)
            body = resp.json() if resp.content else {}
        except (requests.RequestException, ValueError) as exc:
            log.warning("Couldn't reach Slack to finish a sign-in: %s", exc)
            raise LoginError("Couldn't reach Slack to finish signing in. Try again in a minute.") from exc
        if not body.get("ok") or not body.get("id_token"):
            log.warning("Slack sign-in token exchange failed: %s", body.get("error"))
            raise LoginError("Slack didn't confirm the sign-in. Try again.")
        return self.verify_id_token(body["id_token"], nonce)

    def verify_id_token(self, token: str, nonce: str) -> dict[str, Any]:
        try:
            kid = jwt.get_unverified_header(token).get("kid")
            key = self._key_for(kid)
            claims = jwt.decode(token, key=key, algorithms=["RS256"], audience=self.settings.slack_client_id,
                                issuer=ISSUER, leeway=60, options={"require": ["exp", "iat", "aud", "iss"]})
        except jwt.PyJWTError as exc:
            log.warning("Rejected a Slack ID token: %s", exc)
            raise LoginError("That sign-in couldn't be verified. Try again.") from exc
        except requests.RequestException as exc:
            log.warning("Couldn't download Slack's signing keys: %s", exc)
            raise LoginError("Couldn't reach Slack to finish signing in. Try again in a minute.") from exc
        if claims.get("nonce") != nonce:
            raise LoginError("That sign-in link expired. Try again.")
        if not self.settings.slack_team_id:
            # Fail closed: without the team's workspace ID we can't tell team members from outsiders.
            raise LoginError("Sign-in isn't ready: the app couldn't confirm the team's Slack workspace. "
                             "Ask an admin to check the Slack bot token.")
        if claims.get(TEAM_CLAIM) != self.settings.slack_team_id:
            raise LoginError("Sign in with the team's Slack workspace. That account belongs to a different workspace.")
        if not claims.get(USER_CLAIM):
            raise LoginError("Slack didn't share your user ID. Try again.")
        return claims

    def _download_jwks(self) -> dict[str, Any]:
        resp = self.session.get(JWKS_URL, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def _key_for(self, kid: str | None):
        for attempt in range(2):
            if self._jwks is None or attempt == 1 or time.monotonic() - self._jwks_at > 3600:
                self._jwks = self._fetch_jwks()
                self._jwks_at = time.monotonic()
            for jwk in self._jwks.get("keys", []):
                if kid is None or jwk.get("kid") == kid:
                    return jwt.PyJWK(jwk).key
        raise jwt.InvalidTokenError("No matching signing key")


def profile_from_claims(claims: dict[str, Any]) -> dict[str, Any]:
    name = claims.get("name") or claims.get("given_name") or claims.get("email") or claims[USER_CLAIM]
    return {
        "slack_user_id": claims[USER_CLAIM],
        "team_id": claims.get(TEAM_CLAIM),
        "name": name,
        "email": claims.get("email"),
        "avatar_url": (claims.get("picture") or claims.get("https://slack.com/user_image_72")
                       or claims.get("https://slack.com/user_image_48")),
    }
