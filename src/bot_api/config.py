"""Persistent settings in ``$XDG_CONFIG_HOME/bot-api/config.toml`` (or ``$BOT_API_CONFIG``)."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

import tomli_w
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .contract import Effort

DEFAULT_SYSTEM_PROMPT = (
    "You are a knowledgeable, precise assistant. Answer the user's message directly. "
    "Use Markdown only where it improves clarity. You have no tools and no file access; "
    "never mention the terminal, files, or tools."
)


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    model: str = Field(default="sonnet", description="Model alias or full ID.")
    skill: str | None = Field(default=None, description="Default skill name, if any.")
    effort: Effort | None = Field(default=None, description="None = model default.")
    thinking_enabled: bool = True
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    claude_bin: str | None = Field(default=None, description="Path to the claude executable.")
    timeout_s: float = Field(default=300.0, gt=0)


def config_path() -> Path:
    if explicit := os.environ.get("BOT_API_CONFIG"):
        return Path(explicit).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "bot-api" / "config.toml"


def load() -> Settings:
    path = config_path()
    if not path.exists():
        return Settings()
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    return Settings.model_validate(data)


def save(settings: Settings, path: Path | None = None) -> Path:
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = settings.model_dump(mode="json", exclude_none=True)
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(tomli_w.dumps(payload), encoding="utf-8")
    tmp.replace(path)
    return path


def set_value(settings: Settings, key: str, raw: str) -> Settings:
    """Apply ``key=raw`` with the same validation the model enforces. Raises ValueError."""
    if key not in Settings.model_fields:
        raise ValueError(f"unknown setting {key!r}; valid: {', '.join(Settings.model_fields)}")
    value: Any = raw
    if raw.lower() in {"none", "null", ""}:
        value = None
    elif raw.lower() in {"true", "false"}:
        value = raw.lower() == "true"
    try:
        return Settings.model_validate(settings.model_dump() | {key: value})
    except ValidationError as exc:
        raise ValueError(exc.errors()[0]["msg"]) from exc
