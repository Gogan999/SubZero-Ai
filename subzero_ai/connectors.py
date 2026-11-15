"""Connectors: the team's GitHub repository, which Code downloads as a zip, and the admin check that the
repository and token work. (The chat's file picker and Google Drive links were removed in 0.11.)"""

from __future__ import annotations

import re
from collections.abc import Callable
from urllib.parse import quote

import requests

from .config import Settings

REPO_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
BRANCH_RE = re.compile(r"[A-Za-z0-9._/-]{1,100}")
TIMEOUT = 20


# --------------------------------------------------------------------------- errors and helpers
class ConnectorError(Exception):
    """A problem fetching a file, worded for the student."""


def _read_limited(resp: requests.Response, limit: int, what: str) -> bytes:
    data = bytearray()
    for chunk in resp.iter_content(64 * 1024):
        data += chunk
        if len(data) > limit:
            raise ConnectorError(f"{what} is over {limit // (1024 * 1024)} MB, too big to add.")
    return bytes(data)


# --------------------------------------------------------------------------- GitHub
class GitHub:
    API = "https://api.github.com"

    def __init__(self, settings: Settings, session_factory: Callable[[], requests.Session] = requests.Session):
        self.settings = settings
        self.session_factory = session_factory

    @property
    def configured(self) -> bool:
        return bool(REPO_RE.fullmatch(self.settings.connector_github_repo or ""))

    def _get(self, path: str, *, raw: bool = False, repo: str | None = None, branch: str | None = None,
             token: str | None = None) -> requests.Response:
        repo = repo or self.settings.connector_github_repo
        branch = self.settings.connector_github_branch if branch is None else branch
        token = self.settings.connector_github_token if token is None else token
        url = f"{self.API}/repos/{repo}/contents/{quote(path, safe='/')}"
        headers = {"Accept": "application/vnd.github.raw" if raw else "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "SubZero-AI"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            resp = self.session_factory().get(url, params={"ref": branch} if branch else None, headers=headers,
                                              timeout=TIMEOUT, stream=raw)
        except requests.RequestException as exc:
            raise ConnectorError("Couldn't reach GitHub. Try again in a minute.") from exc
        if resp.status_code == 404:
            raise ConnectorError("GitHub says that isn't there, or the token can't see it.")
        if resp.status_code in (401, 403):
            if resp.headers.get("x-ratelimit-remaining") == "0":
                raise ConnectorError("GitHub's limit for this hour is used up. An admin can add a token in Settings "
                                     "to raise it.")
            raise ConnectorError("GitHub refused the request. An admin should check the token in Settings.")
        if resp.status_code >= 400:
            raise ConnectorError(f"GitHub answered with an error ({resp.status_code}). Try again in a minute.")
        return resp

    def check(self, repo: str, branch: str, token: str) -> None:
        """Raises ConnectorError unless the repository can be read with these details."""
        self._get("", repo=repo, branch=branch, token=token)

    def download_zip(self, limit: int) -> bytes:
        """The whole repository at the configured branch, as GitHub's zip archive (no git needed)."""
        url = f"{self.API}/repos/{self.settings.connector_github_repo}/zipball/{quote(self.settings.connector_github_branch, safe='/')}"
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "SubZero-AI"}
        if self.settings.connector_github_token:
            headers["Authorization"] = f"Bearer {self.settings.connector_github_token}"
        try:
            resp = self.session_factory().get(url.rstrip("/"), headers=headers, timeout=120, stream=True)
        except requests.RequestException as exc:
            raise ConnectorError("Couldn't reach GitHub. Try again in a minute.") from exc
        if resp.status_code >= 400:
            raise ConnectorError(f"GitHub couldn't send the repository ({resp.status_code}). An admin should check "
                                 "the repository and token in Settings, Connectors.")
        return _read_limited(resp, limit, "The repository")
