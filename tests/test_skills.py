from __future__ import annotations

from pathlib import Path

import pytest

from bot_api import AskRequest, Effort, ErrorCode, ThinkingConfig, ask, ask_result
from bot_api.config import Settings
from bot_api.skillset import SkillError, export, load_all, parse, resolve, user_dir
from tests.conftest import FakeClaude


def test_bundled_skills_load() -> None:
    skills = load_all()
    assert {"ja", "zh"} <= set(skills)
    ja = skills["ja"]
    assert ja.source == "bundled" and ja.model == "sonnet" and ja.thinking_enabled is False
    assert "Translation" in ja.prompt and len(ja.prompt) < 1600


def test_parse_errors() -> None:
    with pytest.raises(SkillError):
        parse("no front matter", source="user", path="x")
    with pytest.raises(SkillError):
        parse("+++\nname = 'x'\n", source="user", path="x")
    with pytest.raises(SkillError):
        parse("+++\nname = 'x'\nbogus = 1\n+++\nprompt", source="user", path="x")
    with pytest.raises(SkillError):
        resolve("nope")


def test_user_skill_shadows_bundled(isolated_config: Path) -> None:
    folder = user_dir()
    folder.mkdir(parents=True)
    (folder / "ja.md").write_text('+++\nname = "ja"\nmodel = "opus"\n+++\nMINE', encoding="utf-8")
    (folder / "kr.md").write_text('+++\nname = "kr"\n+++\nKorean', encoding="utf-8")
    skills = load_all()
    assert skills["ja"].source == "user" and skills["ja"].prompt == "MINE"
    assert skills["kr"].model is None


def test_export_copies_bundled(isolated_config: Path) -> None:
    path = export("zh")
    assert path == user_dir() / "zh.md" and path.read_text().startswith("+++")


def test_skill_defaults_and_precedence(fake_claude: FakeClaude) -> None:
    resp = ask(AskRequest(prompt="q", skill="ja"), Settings(model="opus", effort=Effort.max))
    argv = fake_claude.argv()
    assert argv[argv.index("--model") + 1] == "sonnet"  # skill beats settings
    assert argv[argv.index("--effort") + 1] == "max"  # skill has no effort -> settings
    assert fake_claude.log()["env"]["MAX_THINKING_TOKENS"] == "0"  # skill turns thinking off
    assert argv[argv.index("--system-prompt") + 1].startswith("You explain Japanese")
    assert resp.skill == "ja"

    ask(
        AskRequest(prompt="q", skill="ja", model="opus", thinking=ThinkingConfig(enabled=True)),
        Settings(),
    )
    argv = fake_claude.argv()
    assert argv[argv.index("--model") + 1] == "opus"  # request beats skill
    assert fake_claude.log()["env"]["MAX_THINKING_TOKENS"] is None


def test_configured_default_skill(fake_claude: FakeClaude) -> None:
    ask(AskRequest(prompt="q"), Settings(skill="zh"))
    argv = fake_claude.argv()
    assert argv[argv.index("--system-prompt") + 1].startswith("You explain Mandarin")


def test_unknown_skill_is_invalid_request(fake_claude: FakeClaude) -> None:
    result = ask_result(AskRequest(prompt="q", skill="nope"), Settings())
    assert result.ok is False and result.error.code == ErrorCode.invalid_request  # type: ignore[union-attr]
