"""Settings, loaded from config.env with environment variables taking priority."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field, fields
from pathlib import Path


def parse_env_file(path: Path) -> dict[str, str]:
    """Read KEY=VALUE lines. Blank lines and lines starting with # are ignored."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def _as_bool(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_set(value: str) -> frozenset[str]:
    return frozenset(part.strip() for part in str(value).split(",") if part.strip())


def default_data_dir() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "SubZeroAI"
    return Path.cwd() / "data"


@dataclass
class Settings:
    data_dir: Path = field(default_factory=default_data_dir)
    config_file: str = ""  # which config.env was read (set by load_settings)
    public_url: str = "http://localhost:8787"
    host: str = "127.0.0.1"
    port: int = 8787
    session_secret: str = ""
    app_name: str = "SubZero AI"
    timezone: str = "America/Chicago"

    # Claude API
    anthropic_api_key: str = ""
    anthropic_base_url: str = "https://api.anthropic.com"
    model_default: str = "claude-sonnet-5-5"
    model_hard: str = "claude-opus-5-5"  # mentors can switch a conversation to this for hard problems
    model_titles: str = "claude-haiku-5-5"
    effort_default: str = "medium"
    effort_high: str = "high"  # for explainers and messages with files attached (usually code reviews)
    max_tokens_chat: int = 8000
    max_tokens_explainer: int = 16000
    long_chat_tokens: int = 150000  # past this, suggest starting a fresh chat
    fake_claude: bool = False

    # Slack
    slack_bot_token: str = ""
    slack_app_token: str = ""
    slack_client_id: str = ""
    slack_client_secret: str = ""
    slack_team_id: str = ""
    slack_approvals_channel: str = ""
    admin_slack_ids: frozenset[str] = frozenset()
    allow_slack_guests: bool = False  # guest accounts may be parents, sponsors, alumni, or other teams

    # Approval rules
    mentors_need_approval: bool = False
    limit_pending_per_student: int = 3
    limit_approved_per_day: int = 25
    reminder_minutes: int = 120
    show_pending_to_team: bool = False
    show_rejected_to_team: bool = False

    # Attachments (images and other files are capped at 5 MB each, 5 per message)
    max_text_file_kb: int = 200

    # Local testing only. Never turn this on for the public site.
    dev_login: bool = False

    # ------------------------------------------------------------------ helpers
    @property
    def slack_enabled(self) -> bool:
        return bool(self.slack_bot_token)

    @property
    def socket_mode_enabled(self) -> bool:
        return bool(self.slack_bot_token and self.slack_app_token)

    @property
    def slack_login_enabled(self) -> bool:
        return bool(self.slack_client_id and self.slack_client_secret)

    @property
    def https(self) -> bool:
        return self.public_url.lower().startswith("https://")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "data" / "subzero.db"

    def url(self, path: str) -> str:
        return self.public_url.rstrip("/") + path


_CONVERTERS = {
    bool: _as_bool,
    int: int,
    frozenset[str]: _as_set,
    "bool": _as_bool,
    "int": int,
    "frozenset[str]": _as_set,
    "Path": Path,
}


def load_settings(env_file: str | os.PathLike | None = None, *, search_cwd: bool = True, **overrides) -> Settings:
    """Build Settings from defaults, then config.env, then environment variables, then overrides.

    config.env is the first of: env_file, $SUBZERO_CONFIG, DATA_DIR/config.env, ./config.env."""
    data_dir = Path(os.environ.get("DATA_DIR") or overrides.get("data_dir") or default_data_dir())

    candidates: list[Path] = []
    if env_file:
        candidates.append(Path(env_file))
    if os.environ.get("SUBZERO_CONFIG"):
        candidates.append(Path(os.environ["SUBZERO_CONFIG"]))
    candidates.append(data_dir / "config.env")
    if search_cwd:
        candidates.append(Path.cwd() / "config.env")

    file_values: dict[str, str] = {}
    config_file = ""
    for path in candidates:
        if path.exists():
            file_values = parse_env_file(path)
            config_file = str(path)
            break

    if "DATA_DIR" in file_values and not os.environ.get("DATA_DIR") and "data_dir" not in overrides:
        data_dir = Path(file_values["DATA_DIR"])

    settings = Settings(data_dir=data_dir, config_file=config_file)
    for f in fields(Settings):
        if f.name in ("data_dir", "config_file"):
            continue
        key = f.name.upper()
        raw = os.environ.get(key, file_values.get(key))
        if raw is None:
            continue
        converter = _CONVERTERS.get(f.type, str)
        setattr(settings, f.name, converter(raw))

    for name, value in overrides.items():
        setattr(settings, name, value)

    settings.data_dir = Path(settings.data_dir)
    for sub in ("data", "files", "backups", "logs"):
        (settings.data_dir / sub).mkdir(parents=True, exist_ok=True)

    if not settings.session_secret:
        secret_file = settings.data_dir / "data" / "session.secret"
        if secret_file.exists():
            settings.session_secret = secret_file.read_text(encoding="utf-8").strip()
        else:
            settings.session_secret = secrets.token_urlsafe(48)
            secret_file.write_text(settings.session_secret, encoding="utf-8")
    return settings
