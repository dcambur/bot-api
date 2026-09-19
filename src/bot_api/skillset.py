"""Skills: named presets of system prompt + model/thinking defaults.

A skill is a Markdown file with TOML front matter between ``+++`` lines::

    +++
    name = "ja"
    description = "..."
    model = "sonnet"          # optional
    thinking_enabled = false  # optional
    effort = "low"            # optional
    +++
    <system prompt>

Bundled skills live in :mod:`bot_api.skills`; user skills in
``<config dir>/skills/<name>.md`` and override bundled ones with the same name.
"""

from __future__ import annotations

import tomllib
from importlib import resources
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from . import config
from .contract import Effort

FENCE = "+++"


class Skill(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    language: str | None = None
    model: str | None = None
    thinking_enabled: bool | None = None
    effort: Effort | None = None
    prompt: str
    source: Literal["bundled", "user"]
    path: str


class SkillError(ValueError):
    pass


def parse(text: str, *, source: Literal["bundled", "user"], path: str) -> Skill:
    lines = text.splitlines()
    if not lines or lines[0].strip() != FENCE:
        raise SkillError(f"{path}: missing '+++' front matter")
    try:
        end = lines.index(FENCE, 1)
    except ValueError as exc:
        raise SkillError(f"{path}: unterminated front matter") from exc
    try:
        meta = tomllib.loads("\n".join(lines[1:end]))
        return Skill.model_validate(
            meta | {"prompt": "\n".join(lines[end + 1 :]).strip(), "source": source, "path": path}
        )
    except (tomllib.TOMLDecodeError, ValidationError) as exc:
        raise SkillError(f"{path}: {exc}") from exc


def user_dir() -> Path:
    return config.config_path().parent / "skills"


def _bundled() -> dict[str, Skill]:
    out: dict[str, Skill] = {}
    for entry in resources.files("bot_api.skills").iterdir():
        if entry.name.endswith(".md"):
            skill = parse(entry.read_text(encoding="utf-8"), source="bundled", path=str(entry))
            out[skill.name] = skill
    return out


def _user() -> dict[str, Skill]:
    out: dict[str, Skill] = {}
    folder = user_dir()
    if folder.is_dir():
        for file in sorted(folder.glob("*.md")):
            skill = parse(file.read_text(encoding="utf-8"), source="user", path=str(file))
            out[skill.name] = skill
    return out


def load_all() -> dict[str, Skill]:
    """All skills by name; a user skill shadows a bundled one."""
    merged = _bundled() | _user()
    return dict(sorted(merged.items()))


def resolve(name: str) -> Skill:
    skills = load_all()
    if name not in skills:
        raise SkillError(
            f"unknown skill {name!r}; available: {', '.join(sorted(skills)) or 'none'}"
        )
    return skills[name]


def export(name: str) -> Path:
    """Copy a skill into the user dir so it can be edited. Returns the new path."""
    skill = resolve(name)
    target = user_dir() / f"{name}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(Path(skill.path).read_text(encoding="utf-8"), encoding="utf-8")
    return target
