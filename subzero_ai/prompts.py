"""Loads the prompt layers (safety and conduct, team context, output formats) for chats and Code sessions."""

from __future__ import annotations

from pathlib import Path

from .config import Settings

PACKAGE_PROMPTS = Path(__file__).parent / "prompts"
PROMPT_FILES = ("safety.md", "team.md", "formats.md")


def load_safety_prompt(settings: Settings) -> str:
    """Just the safety and conduct layer (the mentor's edited copy in DATA_DIR/prompts wins), for Code sessions."""
    override = settings.data_dir / "prompts" / "safety.md"
    path = override if override.exists() else PACKAGE_PROMPTS / "safety.md"
    return path.read_text(encoding="utf-8").strip().replace("{app_name}", settings.app_name)


def load_system_prompt(settings: Settings) -> str:
    """The three layers: safety and conduct, team context, output formats.
    A file with the same name in DATA_DIR/prompts replaces the built-in one."""
    parts = []
    for name in PROMPT_FILES:
        override = settings.data_dir / "prompts" / name
        path = override if override.exists() else PACKAGE_PROMPTS / name
        parts.append(path.read_text(encoding="utf-8").strip())
    text = "\n\n".join(parts)
    return text.replace("{app_name}", settings.app_name)
