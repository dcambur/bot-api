from __future__ import annotations

import json
import time

import pytest

from bot_api import (
    AskRequest,
    BotApiError,
    Effort,
    ErrorCode,
    ThinkingConfig,
    ask,
    ask_result,
    stream_ask,
    usage,
)
from bot_api.config import Settings
from bot_api.runner import build_command, find_claude
from tests.conftest import FakeClaude


def _flag(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def test_ask_sends_prompt_on_stdin_with_isolation_flags(fake_claude: FakeClaude) -> None:
    resp = ask(AskRequest(prompt="こんにちは"), Settings())
    assert resp.ok and resp.text == "echo: こんにちは"
    assert resp.model == "sonnet" and resp.session_id is None  # not persisted: not resumable
    assert resp.usage.thinking_tokens == 3 and resp.cost_usd == 0.001
    log = fake_claude.log()
    argv = log["argv"]
    assert log["stdin"] == "こんにちは"
    assert argv[:3] == ["-p", "--output-format", "json"]
    assert _flag(argv, "--tools") == "" and _flag(argv, "--max-turns") == "1"
    assert _flag(argv, "--setting-sources") == ""
    assert "--no-session-persistence" in argv and "--bare" not in argv
    assert "--safe-mode" in argv
    assert _flag(argv, "--model") == "sonnet" and "--effort" not in argv
    assert log["env"]["MAX_THINKING_TOKENS"] is None
    assert log["cwd"].endswith("workdir")


def test_request_overrides_settings(fake_claude: FakeClaude) -> None:
    settings = Settings(model="sonnet", effort=Effort.low, system_prompt="base")
    req = AskRequest(
        prompt="q",
        model="opus",
        system_prompt="custom",
        thinking=ThinkingConfig(enabled=False, effort=Effort.max),
        output_schema={"type": "object"},
        persist_session=True,
    )
    resp = ask(req, settings)
    log = fake_claude.log()
    argv = log["argv"]
    assert _flag(argv, "--model") == "opus"
    assert _flag(argv, "--effort") == "max"
    assert _flag(argv, "--system-prompt") == "custom"
    assert _flag(argv, "--json-schema") == '{"type": "object"}'
    assert "--no-session-persistence" not in argv
    assert log["env"]["MAX_THINKING_TOKENS"] == "0"
    assert resp.model == "opus" and resp.session_id == "sess-123"  # persisted: resumable


def test_resume_session(fake_claude: FakeClaude) -> None:
    ask(AskRequest(prompt="more", session_id="abc"), Settings())
    argv = fake_claude.argv()
    assert _flag(argv, "--resume") == "abc" and "--no-session-persistence" not in argv


def test_env_overrides_are_stripped(fake_claude: FakeClaude) -> None:
    fake_claude.monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "max")
    fake_claude.monkeypatch.setenv("MAX_THINKING_TOKENS", "0")
    ask(AskRequest(prompt="q"), Settings(effort=Effort.low, thinking_enabled=True))
    env = fake_claude.log()["env"]
    assert env["CLAUDE_CODE_EFFORT_LEVEL"] is None and env["MAX_THINKING_TOKENS"] is None


def test_auth_env_is_stripped_unless_kept(fake_claude: FakeClaude) -> None:
    fake_claude.monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-nope")
    fake_claude.monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    ask(AskRequest(prompt="q"), Settings())
    env = fake_claude.log()["env"]
    assert env["ANTHROPIC_API_KEY"] is None and env["CLAUDE_CODE_USE_BEDROCK"] is None
    ask(AskRequest(prompt="q"), Settings(keep_auth_env=True))
    env = fake_claude.log()["env"]
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-nope" and env["CLAUDE_CODE_USE_BEDROCK"] == "1"


def test_safe_mode_only_when_supported_and_enabled(fake_claude: FakeClaude) -> None:
    ask(AskRequest(prompt="q"), Settings(safe_mode=False))
    assert "--safe-mode" not in fake_claude.argv()
    fake_claude.monkeypatch.setenv("FAKE_CLAUDE_OLD", "1")  # a claude without the flag
    ask(AskRequest(prompt="q"), Settings())
    assert "--safe-mode" not in fake_claude.argv()


def test_safe_mode_support_is_cached_per_binary(fake_claude: FakeClaude) -> None:
    from bot_api.runner import supports_safe_mode

    assert supports_safe_mode(fake_claude.bin) is True
    fake_claude.log_path.unlink()
    assert supports_safe_mode(fake_claude.bin) is True
    assert not fake_claude.log_path.exists()  # answered from cli-cache.json, no --help spawn


def test_unsupported_effort_is_rejected_before_spawning(fake_claude: FakeClaude) -> None:
    with pytest.raises(BotApiError) as exc:
        ask(AskRequest(prompt="q", model="claude-opus-4-6"), Settings(effort=Effort.xhigh))
    assert exc.value.code == ErrorCode.unsupported_effort
    assert not fake_claude.log_path.exists()


def test_warnings_for_unknown_model_and_fable_thinking(fake_claude: FakeClaude) -> None:
    resp = ask(AskRequest(prompt="q", model="my-custom-id"), Settings())
    assert any("not in the catalog" in w for w in resp.warnings)
    resp = ask(AskRequest(prompt="q", model="fable"), Settings(thinking_enabled=False))
    assert any("cannot be turned off" in w for w in resp.warnings)


def test_structured_output_is_surfaced(fake_claude: FakeClaude) -> None:
    fake_claude.respond(structured_output={"translation": "cat"})
    resp = ask(AskRequest(prompt="q", output_schema={"type": "object"}), Settings())
    assert resp.structured == {"translation": "cat"}


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (
            {"is_error": True, "api_error_status": 404, "result": "issue with the selected model"},
            ErrorCode.invalid_model,
        ),
        (
            {"is_error": True, "api_error_status": 401, "result": "Please log in"},
            ErrorCode.not_authenticated,
        ),
        (
            {"is_error": True, "result": "Invalid API key · Please run /login"},
            ErrorCode.not_authenticated,
        ),
        ({"is_error": True, "api_error_status": 500, "result": "boom"}, ErrorCode.claude_error),
    ],
)
def test_error_mapping(
    fake_claude: FakeClaude, payload: dict[str, object], code: ErrorCode
) -> None:
    fake_claude.respond(**payload)
    result = ask_result(AskRequest(prompt="q"), Settings())
    assert result.ok is False and result.error.code == code  # type: ignore[union-attr]


def test_noise_before_json_is_tolerated(fake_claude: FakeClaude) -> None:
    fake_claude.monkeypatch.setenv("FAKE_CLAUDE_NOISE", "1")
    assert ask(AskRequest(prompt="q"), Settings()).text == "echo: q"


def test_timeout(fake_claude: FakeClaude) -> None:
    fake_claude.monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "2")
    with pytest.raises(BotApiError) as exc:
        ask(AskRequest(prompt="q", timeout_s=0.2), Settings())
    assert exc.value.code == ErrorCode.timeout


def test_stream_timeout_is_enforced_while_streaming(fake_claude: FakeClaude) -> None:
    fake_claude.monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "3")
    t0 = time.monotonic()
    with pytest.raises(BotApiError) as exc:
        stream_ask(AskRequest(prompt="q", timeout_s=0.3), lambda _: None, Settings())
    assert exc.value.code == ErrorCode.timeout
    assert time.monotonic() - t0 < 2, "the deadline must cover the read loop, not just wait()"


def test_usage_ledger_records_success_and_failure(fake_claude: FakeClaude) -> None:
    ask(AskRequest(prompt="hello", skill="ja", model="opus"), Settings())
    fake_claude.respond(is_error=True, api_error_status=500, result="boom")
    ask_result(AskRequest(prompt="q"), Settings())
    fake_claude.monkeypatch.delenv("FAKE_CLAUDE_RESPONSE")
    ask(AskRequest(prompt="quiet"), Settings(usage_log=False))
    lines = [json.loads(line) for line in usage.path().read_text().splitlines()]
    assert len(lines) == 2
    ok, bad = lines
    assert ok["ok"] is True and ok["skill"] == "ja" and ok["model"] == "opus"
    assert ok["output_tokens"] == 5 and ok["thinking_tokens"] == 3 and ok["prompt_chars"] == 5
    assert bad["ok"] is False and bad["error"] == "claude_error"
    summary = usage.summarize(usage.read())
    assert summary["calls"] == 2 and summary["errors"] == 1 and summary["output_tokens"] == 5
    assert summary["by_skill"] == {"ja": 1, "(none)": 1}


def test_skill_none_skips_configured_default(fake_claude: FakeClaude) -> None:
    resp = ask(AskRequest(prompt="q", skill="none"), Settings(skill="ja", system_prompt="BASE"))
    argv = fake_claude.argv()
    assert argv[argv.index("--system-prompt") + 1] == "BASE" and resp.skill is None


def test_missing_binary(no_claude: None) -> None:
    with pytest.raises(BotApiError) as exc:
        find_claude(None)
    assert exc.value.code == ErrorCode.claude_not_found
    result = ask_result(AskRequest(prompt="q"), Settings())
    assert result.ok is False and result.error.code == ErrorCode.claude_not_found  # type: ignore[union-attr]


def test_stream_ask_delivers_chunks_and_final_result(fake_claude: FakeClaude) -> None:
    chunks: list[str] = []
    resp = stream_ask(AskRequest(prompt="hello"), chunks.append, Settings())
    assert "".join(chunks) == "echo: hello" and len(chunks) == 2
    assert resp.text == "echo: hello" and resp.session_id is None
    argv = fake_claude.argv()
    assert _flag(argv, "--output-format") == "stream-json"
    assert "--verbose" in argv and "--include-partial-messages" in argv


def test_stream_ask_maps_errors(fake_claude: FakeClaude) -> None:
    fake_claude.respond(is_error=True, api_error_status=404, result="no such model")
    with pytest.raises(BotApiError) as exc:
        stream_ask(AskRequest(prompt="q"), lambda _: None, Settings())
    assert exc.value.code == ErrorCode.invalid_model


def test_build_command_is_deterministic(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from bot_api.runner import resolve_effective

    req = AskRequest(prompt="x", model="haiku")
    eff = resolve_effective(req, Settings())
    cmd = build_command(tmp_path / "claude", req, eff, stream=False)
    assert cmd == build_command(tmp_path / "claude", req, eff, stream=False)
    assert cmd.count("--tools") == 1
