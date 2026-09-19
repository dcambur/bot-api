from __future__ import annotations

import json
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

FAKE = Path(__file__).parent / "fake_claude.py"


@dataclass
class FakeClaude:
    bin: Path
    log_path: Path
    monkeypatch: pytest.MonkeyPatch

    def log(self) -> dict[str, Any]:
        return json.loads(self.log_path.read_text())

    def argv(self) -> list[str]:
        return list(self.log()["argv"])

    def respond(self, **payload: Any) -> None:
        self.monkeypatch.setenv("FAKE_CLAUDE_RESPONSE", json.dumps(payload))


@pytest.fixture
def fake_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeClaude:
    shim = tmp_path / "claude"
    shutil.copy(FAKE, shim)
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    log_path = tmp_path / "invocation.json"
    monkeypatch.setenv("BOT_API_CLAUDE_BIN", str(shim))
    monkeypatch.setenv("BOT_API_CONFIG", str(tmp_path / "cfg" / "config.toml"))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log_path))
    for var in (
        "FAKE_CLAUDE_RESPONSE",
        "FAKE_CLAUDE_SLEEP",
        "FAKE_CLAUDE_NOISE",
        "MAX_THINKING_TOKENS",
        "CLAUDE_CODE_EFFORT_LEVEL",
        "ANTHROPIC_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)
    return FakeClaude(bin=shim, log_path=log_path, monkeypatch=monkeypatch)


@pytest.fixture
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "cfg" / "config.toml"
    monkeypatch.setenv("BOT_API_CONFIG", str(path))
    return path


@pytest.fixture
def no_claude(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Make every discovery path fail."""
    monkeypatch.delenv("BOT_API_CLAUDE_BIN", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("BOT_API_CONFIG", str(tmp_path / "config.toml"))
    assert os.environ["PATH"] == str(tmp_path)
