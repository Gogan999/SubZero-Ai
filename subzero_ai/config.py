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


# The AI providers an admin can turn on (the Pi backend reaches all of them; the Anthropic backend only the first)
PROVIDERS = ("anthropic", "openai", "google", "openrouter")


def normalize_model_id(value: object) -> str:
    """Model ids are provider/model. Bare Claude ids from older settings become anthropic/claude-...;
    anything else is only trimmed."""
    text = str(value or "").strip()
    return "anthropic/" + text if text.startswith("claude-") else text


def model_provider(value: object) -> str:
    """The provider part of a model id ('' when the id has no provider)."""
    text = normalize_model_id(value)
    return text.split("/", 1)[0] if "/" in text else ""


def _as_providers(value: str) -> tuple[str, ...]:
    """Known providers only, in a fixed order; anthropic if nothing valid is left."""
    wanted = {part.strip().lower() for part in str(value).replace("\n", ",").split(",")}
    return tuple(p for p in PROVIDERS if p in wanted) or ("anthropic",)


def _as_bool(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_set(value: str) -> frozenset[str]:
    return frozenset(part.strip() for part in str(value).replace("\n", ",").split(",") if part.strip())


def _as_list(value: str) -> tuple[str, ...]:
    """Comma-separated in config.env, one per line when saved from the settings page."""
    parts = str(value).split("\n") if "\n" in str(value) else str(value).split(",")
    return tuple(dict.fromkeys(part.strip() for part in parts if part.strip()))


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
    session_secret: str = field(default="", repr=False)
    app_name: str = "SubZero AI"
    timezone: str = "America/Chicago"

    # Claude API
    anthropic_api_key: str = field(default="", repr=False)
    anthropic_base_url: str = "https://api.anthropic.com"
    openai_api_key: str = field(default="", repr=False)
    gemini_api_key: str = field(default="", repr=False)
    openrouter_api_key: str = field(default="", repr=False)
    ai_providers: tuple[str, ...] = ("anthropic",)  # providers admins turned on; the others' models are refused
    model_default: str = "anthropic/claude-sonnet-5-5"  # model ids are provider/model
    model_hard: str = "anthropic/claude-opus-5-5"  # mentors can switch a conversation to this for hard problems
    model_titles: str = "anthropic/claude-haiku-5-5"
    effort_default: str = "medium"
    effort_high: str = "high"  # for explainers and messages with files attached (usually code reviews)
    max_tokens_chat: int = 8000
    max_tokens_explainer: int = 16000
    long_chat_tokens: int = 150000  # past this, suggest starting a fresh chat
    fake_claude: bool = False
    ai_backend: str = "anthropic"  # anthropic, pi, or fake (FAKE_CLAUDE=1 also means fake)
    pi_node: str = "node"  # path to Node.js, for the pi backend
    pi_gateway_script: str = ""  # empty means pi_gateway/gateway.mjs next to the subzero_ai folder
    pi_request_timeout: int = 300  # seconds for a whole pi request
    pi_idle_timeout: int = 120  # longest quiet gap in a pi reply, in seconds
    # Web search costs extra for each search, so the whole team shares a daily limit
    web_search_enabled: bool = True
    web_search_daily_limit: int = 50
    research_for_students: bool = False  # Research runs many searches, so it starts as mentors only

    # Slack
    slack_bot_token: str = field(default="", repr=False)
    slack_app_token: str = field(default="", repr=False)
    slack_client_id: str = ""
    slack_client_secret: str = field(default="", repr=False)
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
    # After a mentor approves a message in a chat, the student's next messages in that chat go straight to
    # Claude for this many minutes. Each one sent restarts the clock. 0 turns the window off.
    approval_window_minutes: int = 5
    always_review_slack_ids: frozenset[str] = frozenset()  # students who never get the approval window
    screener_enabled: bool = True  # a small model checks each message in the window; anything it flags waits
    model_screener: str = "anthropic/claude-haiku-5-5"
    # The model that grades the Model safety check's open-ended cases (refusals). Not a saved setting.
    model_check_judge: str = "anthropic/claude-sonnet-5-5"

    # Connectors: add files to a message from the team's GitHub repository or a shared Google Drive link
    connector_github_repo: str = ""  # owner/name
    connector_github_branch: str = ""  # empty means the repository's default branch
    # only for a private repository: a fine-grained token with read-only Contents
    connector_github_token: str = field(default="", repr=False)
    connector_drive_links: bool = True

    # Code: Claude works in a copy of the GitHub repository above; a mentor approves each change and command
    code_commands: tuple[str, ...] = ("gradlew",)  # programs Claude may ask to run, by name
    code_command_timeout: int = 600  # seconds

    # Attachments (images and other files are capped at 5 MB each, 5 per message)
    max_text_file_kb: int = 200

    subteams: tuple[str, ...] = ("Programming", "Fab and design", "Business and outreach")

    # Data kept on the mini PC
    retention_days: int = 730  # delete chats with no activity for this long (two seasons); 0 keeps them forever
    backup_dir: str = ""  # defaults to DATA_DIR/backups; point it at a synced OneDrive or Google Drive folder
    backup_hour: int = 2  # nightly backup at this hour, local time
    backup_keep_days: int = 14

    # Updates: the admin page says when this GitHub repository has a newer release
    update_repo: str = "Gogan999/SubZero-Ai"
    check_for_updates: bool = True

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

    @property
    def backups_path(self) -> Path:
        return Path(self.backup_dir) if self.backup_dir else self.data_dir / "backups"

    @property
    def config_path(self) -> Path:
        """Where settings are saved: the config.env that was read, or DATA_DIR/config.env."""
        return Path(self.config_file) if self.config_file else self.data_dir / "config.env"

    def url(self, path: str) -> str:
        return self.public_url.rstrip("/") + path


_CONVERTERS = {
    bool: _as_bool,
    int: int,
    frozenset[str]: _as_set,
    "bool": _as_bool,
    "int": int,
    "frozenset[str]": _as_set,
    "tuple[str, ...]": _as_list,
    "Path": Path,
}


MODEL_SETTINGS = ("model_default", "model_hard", "model_titles", "model_screener")
MODEL_ID_SETTINGS = MODEL_SETTINGS + ("model_check_judge",)  # every setting that holds a model id


def convert(name: str, raw: str):
    """Turn a text value into the type of the named setting."""
    f = next(f for f in fields(Settings) if f.name == name)
    if name in MODEL_ID_SETTINGS:
        return normalize_model_id(raw)
    if name == "ai_providers":
        return _as_providers(raw)
    return _CONVERTERS.get(f.type, str)(raw)


def update_env_file(path: Path, updates: dict[str, str]) -> None:
    """Set KEY=value lines in config.env, keeping its notes and order. Keys it doesn't have yet go at the end.
    The file is replaced in one step, so a crash never leaves half a file."""
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
    remaining = dict(updates)
    for value in remaining.values():
        if "\n" in value or "\r" in value:
            raise ValueError("Settings can't contain line breaks.")
    for i, raw in enumerate(lines):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key in remaining:
            lines[i] = f"{key}={remaining.pop(key)}"
    if remaining:
        if lines and lines[-1].strip():
            lines.append("")
        lines += [f"{key}={value}" for key, value in remaining.items()]
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(temp, path)


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
        setattr(settings, f.name, convert(f.name, raw))

    for name, value in overrides.items():
        setattr(settings, name, value)
    for name in MODEL_ID_SETTINGS:
        setattr(settings, name, normalize_model_id(getattr(settings, name)))

    settings.data_dir = Path(settings.data_dir)
    for sub in ("data", "files", "backups", "logs"):
        (settings.data_dir / sub).mkdir(parents=True, exist_ok=True)
    try:
        settings.backups_path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # a missing BACKUP_DIR drive shows up as a failed backup on the admin page, not a failed start

    if not settings.session_secret:
        secret_file = settings.data_dir / "data" / "session.secret"
        if secret_file.exists():
            settings.session_secret = secret_file.read_text(encoding="utf-8").strip()
        else:
            settings.session_secret = secrets.token_urlsafe(48)
            secret_file.write_text(settings.session_secret, encoding="utf-8")
    return settings
