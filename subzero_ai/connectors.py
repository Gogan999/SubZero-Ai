"""Connectors: add files to a message from the team's GitHub repository or from a Google Drive link, instead of
downloading and attaching them.

The files are fetched on the mini PC when the message is sent and stored like any attachment, so mentors see
them on the review card and nothing reaches Claude before the message is approved.

GitHub: an admin names one repository (and, for a private one, a read-only token) in Settings. Everyone can
browse it from the composer. Google Drive: anyone can paste a link to a Doc, Sheet, Slides deck or file that's
shared as "Anyone with the link". There's no Google sign-in, so a file has to be shared that way to work."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlparse

import requests

from .attachments import IMAGE_EXTENSIONS, TEXT_EXTENSIONS, AttachmentError, Prepared, prepare
from .config import Settings

REPO_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
BRANCH_RE = re.compile(r"[A-Za-z0-9._/-]{1,100}")
DRIVE_ID_RE = re.compile(r"[A-Za-z0-9_-]{15,200}")
SUPPORTED = TEXT_EXTENSIONS | IMAGE_EXTENSIONS  # no videos: Claude only sees stills taken in the browser
MAX_FETCH_BYTES = 10 * 1024 * 1024
TIMEOUT = 20
GOOGLE_HOSTS = (".google.com", ".googleusercontent.com")


class ConnectorError(AttachmentError):
    """A problem fetching a file, worded for the student."""


@dataclass
class Entry:
    name: str
    path: str
    kind: str  # "dir" or "file"
    size: int
    supported: bool


def _supported(name: str) -> bool:
    dot = name.rfind(".")
    return dot > 0 and name[dot:].lower() in SUPPORTED


def clean_path(path: str) -> str:
    """A path inside the repository, refusing anything that could step outside it."""
    path = (path or "").strip().strip("/")
    if len(path) > 400 or "\\" in path or any(ord(ch) < 32 for ch in path):
        raise ConnectorError("That isn't a path in the repository.")
    if any(part in ("", ".", "..") for part in path.split("/")) and path:
        raise ConnectorError("That isn't a path in the repository.")
    return path


def _read_limited(resp: requests.Response, limit: int, what: str) -> bytes:
    data = bytearray()
    for chunk in resp.iter_content(64 * 1024):
        data += chunk
        if len(data) > limit:
            raise ConnectorError(f"{what} is over {limit // (1024 * 1024)} MB, too big to add.")
    return bytes(data)


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

    def list(self, path: str = "") -> list[Entry]:
        path = clean_path(path)
        body = self._get(path).json()
        if not isinstance(body, list):
            raise ConnectorError("That's a file, not a folder.")
        entries = [Entry(item["name"], item["path"], "dir" if item["type"] == "dir" else "file",
                         int(item.get("size") or 0), item["type"] == "dir" or _supported(item["name"]))
                   for item in body if item.get("type") in ("dir", "file")]
        return sorted(entries, key=lambda e: (e.kind != "dir", e.name.lower()))

    def fetch(self, path: str, *, max_text_bytes: int) -> Prepared:
        path = clean_path(path)
        if not path or not _supported(path):
            raise ConnectorError(f"{path or 'That'} can't be added. Pick a code, text, PNG or JPG file.")
        resp = self._get(path, raw=True)
        item = prepare(path.rsplit("/", 1)[-1], _read_limited(resp, MAX_FETCH_BYTES, path),
                       max_text_bytes=max_text_bytes)
        item.name = f"{self.settings.connector_github_repo.split('/')[-1]}/{path}"[-120:]
        return item


# Google Docs editors files are exported as text; other Drive files are downloaded as they are.
DRIVE_KINDS = {
    "document": ("Google Doc", "https://docs.google.com/document/d/{id}/export?format=txt", ".txt"),
    "spreadsheets": ("Google Sheet", "https://docs.google.com/spreadsheets/d/{id}/export?format=csv", ".csv"),
    "presentation": ("Google Slides", "https://docs.google.com/presentation/d/{id}/export/txt", ".txt"),
}


def parse_drive_link(link: str) -> tuple[str, str]:
    """(kind, file id) from a Google Drive or Docs link. kind is a DRIVE_KINDS key, or "file"."""
    link = (link or "").strip()
    parsed = urlparse(link if "://" in link else "https://" + link)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("https", "http") or host not in ("docs.google.com", "drive.google.com"):
        raise ConnectorError("Paste a link that starts with https://docs.google.com or https://drive.google.com.")
    parts = [p for p in parsed.path.split("/") if p]
    file_id = ""
    kind = "file"
    if host == "docs.google.com" and len(parts) >= 3 and parts[0] in DRIVE_KINDS and parts[1] == "d":
        kind, file_id = parts[0], parts[2]
    elif host == "drive.google.com" and len(parts) >= 3 and parts[0] == "file" and parts[1] == "d":
        file_id = parts[2]
    elif host == "drive.google.com":
        match = re.search(r"(?:^|&)id=([^&]+)", parsed.query)
        file_id = match.group(1) if match else ""
    if not DRIVE_ID_RE.fullmatch(file_id):
        raise ConnectorError("That doesn't look like a link to one file. Open the file in Drive and use Share, "
                             "Copy link.")
    return kind, file_id


class Drive:
    def __init__(self, settings: Settings, session_factory: Callable[[], requests.Session] = requests.Session):
        self.settings = settings
        self.session_factory = session_factory

    @property
    def enabled(self) -> bool:
        return self.settings.connector_drive_links

    def fetch(self, link: str, *, max_text_bytes: int) -> Prepared:
        kind, file_id = parse_drive_link(link)
        if kind in DRIVE_KINDS:
            label, template, ext = DRIVE_KINDS[kind]
            url = template.format(id=file_id)
        else:
            label, url, ext = "Drive file", f"https://drive.google.com/uc?export=download&id={file_id}", ""
        try:
            resp = self.session_factory().get(url, timeout=TIMEOUT, stream=True, allow_redirects=True,
                                              headers={"User-Agent": "SubZero-AI"})
        except requests.RequestException as exc:
            raise ConnectorError("Couldn't reach Google Drive. Try again in a minute.") from exc
        host = (urlparse(resp.url).hostname or "").lower()
        content_type = resp.headers.get("content-type", "")
        not_shared = ConnectorError(f"That {label} isn't shared as \"Anyone with the link\", so it can't be added. "
                                    "Change its sharing in Drive, or download it and attach it instead.")
        if not any(host == h.lstrip(".") or host.endswith(h) for h in GOOGLE_HOSTS) or "accounts.google" in host:
            raise not_shared
        if resp.status_code in (401, 403, 404) or content_type.startswith("text/html"):
            raise not_shared
        if resp.status_code >= 400:
            raise ConnectorError(f"Google Drive answered with an error ({resp.status_code}). Try again in a minute.")
        data = _read_limited(resp, MAX_FETCH_BYTES, f"That {label}")
        name = _download_name(resp.headers.get("content-disposition", "")) or f"{label}{ext or '.txt'}"
        if ext and not name.lower().endswith(ext):
            name = name.rsplit(".", 1)[0] + ext if "." in name else name + ext
        if not _supported(name):
            raise ConnectorError(f"{name} can't be added. Drive links work for Docs, Sheets, Slides, and code, "
                                 "text, PNG or JPG files.")
        return prepare(name, data, max_text_bytes=max_text_bytes)


def _download_name(disposition: str) -> str:
    match = re.search(r"filename\*=UTF-8''([^;]+)", disposition) or re.search(r'filename="([^"]+)"', disposition)
    if not match:
        return ""
    return unquote(match.group(1)).strip()
