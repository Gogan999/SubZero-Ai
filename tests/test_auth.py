"""Sign in with Slack: the OpenID Connect flow with locally signed ID tokens."""

from __future__ import annotations

import json
import time
import unittest
from urllib.parse import parse_qs, urlparse

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from subzero_ai.auth import ISSUER, LoginError, SlackLogin

from .helpers import Harness

CLIENT_ID = "1234.5678"
PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwks():
    key = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(PRIVATE_KEY.public_key()))
    key.update({"kid": "slack-key-1", "alg": "RS256", "use": "sig"})
    return {"keys": [key]}


def id_token(nonce, *, team="TDEV", user="U0AVA", aud=CLIENT_ID, key=PRIVATE_KEY, expires_in=300, **extra):
    now = int(time.time())
    claims = {"iss": ISSUER, "sub": user, "aud": aud, "iat": now, "exp": now + expires_in, "nonce": nonce,
              "https://slack.com/team_id": team, "https://slack.com/user_id": user,
              "name": "Ava Martinez", "email": "ava@example.org", **extra}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "slack-key-1"})


class FakeResponse:
    def __init__(self, body):
        self.body = body
        self.content = json.dumps(body).encode()

    def json(self):
        return self.body


class FakeSession:
    """Stands in for requests.Session when the app exchanges the sign-in code."""

    def __init__(self):
        self.token = None
        self.posts = []

    def post(self, url, data=None, timeout=None):
        self.posts.append({"url": url, "data": data})
        if self.token is None:
            return FakeResponse({"ok": False, "error": "invalid_code"})
        return FakeResponse({"ok": True, "access_token": "xoxp-1", "id_token": self.token})


class VerifyTokenTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness(slack_client_id=CLIENT_ID, slack_client_secret="secret")
        self.login = SlackLogin(self.h.settings, fetch_jwks=jwks)

    def tearDown(self):
        self.h.close()

    def test_valid_token(self):
        claims = self.login.verify_id_token(id_token("n1"), "n1")
        self.assertEqual(claims["https://slack.com/user_id"], "U0AVA")

    def test_rejections(self):
        cases = {
            "other workspace": (id_token("n1", team="TOTHER"), "different workspace"),
            "wrong nonce": (id_token("n2"), "expired"),
            "other app": (id_token("n1", aud="9999.1"), "couldn't be verified"),
            "expired": (id_token("n1", expires_in=-3600), "couldn't be verified"),
            "forged": (id_token("n1", key=OTHER_KEY), "couldn't be verified"),
            "garbage": ("not.a.token", "couldn't be verified"),
        }
        for name, (token, words) in cases.items():
            with self.subTest(name):
                with self.assertRaises(LoginError) as caught:
                    self.login.verify_id_token(token, "n1")
                self.assertIn(words, str(caught.exception))

    def test_refuses_everyone_when_the_workspace_is_unknown(self):
        self.h.settings.slack_team_id = ""
        with self.assertRaises(LoginError):
            self.login.verify_id_token(id_token("n1"), "n1")

    def test_authorize_url(self):
        url = urlparse(self.login.authorize_url("state-1", "nonce-1"))
        self.assertEqual(f"{url.scheme}://{url.netloc}{url.path}", "https://slack.com/openid/connect/authorize")
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        self.assertEqual(q["scope"], "openid profile email")
        self.assertEqual(q["team"], "TDEV")
        self.assertEqual(q["redirect_uri"], "http://localhost:8787/auth/slack/callback")
        self.assertEqual(q["response_type"], "code")


class SignInFlowTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness(slack_client_id=CLIENT_ID, slack_client_secret="secret")
        self.session = FakeSession()
        self.h.services.login = SlackLogin(self.h.settings, session=self.session, fetch_jwks=jwks)
        self.h.slack.users["U0AVA"] = {"id": "U0AVA", "profile": {"image_72": "https://avatars.slack-edge.com/ava.png"}}
        self.client = self.h.client
        self.client.cookies.clear()

    def tearDown(self):
        self.h.close()

    def begin(self):
        resp = self.client.get("/auth/login", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        q = {k: v[0] for k, v in parse_qs(urlparse(resp.headers["location"]).query).items()}
        return q["state"], q["nonce"]

    def test_team_member_signs_in(self):
        self.assertIn("/auth/login", self.client.get("/login").text)
        state, nonce = self.begin()
        self.session.token = id_token(nonce)
        resp = self.client.get(f"/auth/slack/callback?code=abc&state={state}", follow_redirects=False)
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], "/chats")
        user = self.h.services.store.user_by_slack_id("U0AVA")
        self.assertEqual(user["name"], "Ava Martinez")
        self.assertEqual(user["role"], "student")
        self.assertEqual(user["slack_team_id"], "TDEV")
        self.assertEqual(user["avatar_url"], "https://avatars.slack-edge.com/ava.png")
        self.assertEqual(self.session.posts[0]["data"]["code"], "abc")
        page = self.client.get("/chats")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Ava Martinez", page.text)

    def test_mentor_role_comes_from_the_approvals_channel(self):
        self.h.slack.members = {"U0KIM"}
        state, nonce = self.begin()
        self.session.token = id_token(nonce, user="U0KIM", name="Kim Nguyen")
        self.client.get(f"/auth/slack/callback?code=abc&state={state}")
        self.assertEqual(self.h.services.store.user_by_slack_id("U0KIM")["role"], "mentor")

    def test_admins_named_in_settings(self):
        self.h.settings.admin_slack_ids = frozenset({"U0COACH"})
        state, nonce = self.begin()
        self.session.token = id_token(nonce, user="U0COACH")
        self.client.get(f"/auth/slack/callback?code=abc&state={state}")
        self.assertEqual(self.h.services.store.user_by_slack_id("U0COACH")["role"], "admin")

    def test_account_from_another_workspace_is_refused(self):
        state, nonce = self.begin()
        self.session.token = id_token(nonce, team="TOTHER", user="U0OUT")
        resp = self.client.get(f"/auth/slack/callback?code=abc&state={state}")
        self.assertIn("Sign in with the team&#39;s Slack workspace", resp.text)
        self.assertIsNone(self.h.services.store.user_by_slack_id("U0OUT"))
        self.assertEqual(self.client.get("/chats", follow_redirects=False).status_code, 303)

    def test_forged_state_is_refused(self):
        _, nonce = self.begin()
        self.session.token = id_token(nonce)
        resp = self.client.get("/auth/slack/callback?code=abc&state=forged")
        self.assertIn("That sign-in link expired", resp.text)
        self.assertIsNone(self.h.services.store.user_by_slack_id("U0AVA"))

    def test_cancelled_sign_in(self):
        self.begin()
        resp = self.client.get("/auth/slack/callback?error=access_denied")
        self.assertIn("Sign-in was cancelled", resp.text)

    def test_failed_code_exchange(self):
        state, _ = self.begin()
        resp = self.client.get(f"/auth/slack/callback?code=bad&state={state}")
        self.assertIn("Slack didn&#39;t confirm the sign-in", resp.text)

    def test_returns_to_the_page_they_wanted(self):
        self.client.get("/c/42", follow_redirects=False)
        state, nonce = self.begin()
        self.session.token = id_token(nonce)
        resp = self.client.get(f"/auth/slack/callback?code=abc&state={state}", follow_redirects=False)
        self.assertEqual(resp.headers["location"], "/c/42")

    def test_never_returns_to_another_site(self):
        from subzero_ai.web import SAFE_NEXT
        for target in ("/\\evil.example", "//evil.example", "https://evil.example", "/c/1/../../x", "/chats?x=1"):
            self.assertIsNone(SAFE_NEXT.fullmatch(target), target)
        for target in ("/chats", "/c/12", "/queue"):
            self.assertIsNotNone(SAFE_NEXT.fullmatch(target), target)

    def test_network_trouble_during_sign_in(self):
        import requests

        class DownSession(FakeSession):
            def post(self, url, data=None, timeout=None):
                raise requests.ConnectionError("Slack is down")

        self.h.services.login = SlackLogin(self.h.settings, session=DownSession(), fetch_jwks=jwks)
        state, _ = self.begin()
        resp = self.client.get(f"/auth/slack/callback?code=abc&state={state}")
        self.assertIn("Couldn&#39;t reach Slack to finish signing in", resp.text)

    def test_sign_in_waits_until_the_workspace_is_known(self):
        self.h.settings.slack_team_id = ""

        async def broken_auth_test():
            raise ConnectionError("Slack is down")
        self.h.slack.auth_test = broken_auth_test
        resp = self.client.get("/auth/login")
        self.assertIn("couldn&#39;t reach Slack", resp.text)

    def test_sign_out(self):
        state, nonce = self.begin()
        self.session.token = id_token(nonce)
        page = self.client.get(f"/auth/slack/callback?code=abc&state={state}")
        csrf = page.text.split('name="csrf-token" content="')[1].split('"')[0]
        self.client.post("/logout", data={"csrf": csrf})
        self.assertEqual(self.client.get("/chats", follow_redirects=False).status_code, 303)


if __name__ == "__main__":
    unittest.main()
