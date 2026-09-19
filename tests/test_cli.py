from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from bot_api.cli import app
from bot_api.config import load
from tests.conftest import FakeClaude

runner = CliRunner()


def test_ask_prints_text(fake_claude: FakeClaude) -> None:
    result = runner.invoke(app, ["ask", "what is 2+2"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "echo: what is 2+2"


def test_ask_appends_stdin_to_argument(fake_claude: FakeClaude) -> None:
    result = runner.invoke(app, ["ask", "explain"], input="猫が見ている\n")
    assert result.exit_code == 0
    assert fake_claude.log()["stdin"] == "explain\n\n猫が見ている\n"


def test_ask_json_and_flags(fake_claude: FakeClaude) -> None:
    result = runner.invoke(
        app, ["ask", "q", "--json", "-m", "opus", "--effort", "high", "--no-thinking"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True and payload["text"] == "echo: q"
    argv = fake_claude.argv()
    assert argv[argv.index("--effort") + 1] == "high"
    assert fake_claude.log()["env"]["MAX_THINKING_TOKENS"] == "0"


def test_ask_request_contract_roundtrip(fake_claude: FakeClaude) -> None:
    req = {"prompt": "hi", "model": "sonnet", "thinking": {"effort": "low"}}
    result = runner.invoke(app, ["ask", "--request"], input=json.dumps(req))
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True and payload["session_id"] is None


def test_ask_request_invalid_json_is_contract_error(fake_claude: FakeClaude) -> None:
    result = runner.invoke(app, ["ask", "--request"], input='{"prompt": ""}')
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False and payload["error"]["code"] == "invalid_request"


def test_ask_error_exit_code_and_json(fake_claude: FakeClaude) -> None:
    fake_claude.respond(is_error=True, api_error_status=404, result="issue with the selected model")
    result = runner.invoke(app, ["ask", "q", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.output)["error"]["code"] == "invalid_model"


def test_ask_stream(fake_claude: FakeClaude) -> None:
    result = runner.invoke(app, ["ask", "hello", "--stream"])
    assert result.exit_code == 0 and result.output == "echo: hello\n"


def test_models_list_and_set(fake_claude: FakeClaude) -> None:
    result = runner.invoke(app, ["models", "list"])
    assert result.exit_code == 0 and "claude-sonnet-5" in result.output
    assert result.output.splitlines()[0].startswith("  ID")
    assert runner.invoke(app, ["models", "set", "opus"]).exit_code == 0
    assert load().model == "opus"
    assert runner.invoke(app, ["models", "set", "nope"]).exit_code == 1
    assert runner.invoke(app, ["models", "set", "nope", "--allow-unknown"]).exit_code == 0
    assert load().model == "nope"
    listed = json.loads(runner.invoke(app, ["models", "list", "--json"]).output)
    assert not any(row["current"] for row in listed)


def test_models_set_resets_incompatible_effort(fake_claude: FakeClaude) -> None:
    assert runner.invoke(app, ["models", "set", "opus"]).exit_code == 0
    assert runner.invoke(app, ["thinking", "effort", "xhigh"]).exit_code == 0
    result = runner.invoke(app, ["models", "set", "claude-opus-4-6"])
    assert result.exit_code == 0 and load().effort is None


def test_thinking_commands(fake_claude: FakeClaude) -> None:
    assert runner.invoke(app, ["thinking", "off"]).exit_code == 0
    assert load().thinking_enabled is False
    assert runner.invoke(app, ["thinking", "effort", "max"]).exit_code == 0
    assert load().effort == "max"
    assert runner.invoke(app, ["thinking", "effort", "turbo"]).exit_code == 1
    assert runner.invoke(app, ["thinking", "effort", "default"]).exit_code == 0
    assert load().effort is None
    show = runner.invoke(app, ["thinking", "show"]).output
    assert "thinking: off" in show and "effort: default" in show
    # haiku supports no effort levels at all
    assert runner.invoke(app, ["models", "set", "haiku"]).exit_code == 0
    assert runner.invoke(app, ["thinking", "effort", "low"]).exit_code == 1


def test_config_commands(fake_claude: FakeClaude, tmp_path: Path) -> None:
    assert runner.invoke(app, ["config", "set", "system_prompt", "Tutor."]).exit_code == 0
    assert load().system_prompt == "Tutor."
    assert runner.invoke(app, ["config", "set", "timeout_s", "abc"]).exit_code == 1
    assert runner.invoke(app, ["config", "set", "nope", "1"]).exit_code == 1
    shown = json.loads(runner.invoke(app, ["config", "show", "--json"]).output)
    assert shown["system_prompt"] == "Tutor."
    assert runner.invoke(app, ["config", "path"]).output.strip().endswith("config.toml")
    schema = json.loads(runner.invoke(app, ["config", "schema"]).output)
    assert {"request", "response", "error"} <= set(schema)
    assert "prompt" in schema["request"]["properties"]


def test_doctor(fake_claude: FakeClaude) -> None:
    fake_claude.monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-nope")
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "logged in as t@example.com (max)" in result.output
    assert "safe-mode: on" in result.output
    assert "ANTHROPIC_API_KEY set — stripped" in result.output
    assert "usage log:" in result.output


def test_ask_skill_none_and_flag_warnings(fake_claude: FakeClaude) -> None:
    assert runner.invoke(app, ["skills", "use", "ja"]).exit_code == 0
    result = runner.invoke(app, ["ask", "q", "-k", "none", "-v"])
    assert result.exit_code == 0 and "skill=" not in result.output
    argv = fake_claude.argv()
    assert not argv[argv.index("--system-prompt") + 1].startswith("You explain Japanese")
    result = runner.invoke(app, ["ask", "q", "--render", "html"])
    assert result.exit_code == 0 and "--render has no effect" in result.output
    result = runner.invoke(app, ["ask", "q", "--stream", "--json"])
    assert result.exit_code == 0 and "--stream ignored" in result.output


def test_chat_resumes_one_session(fake_claude: FakeClaude) -> None:
    result = runner.invoke(app, ["chat", "-m", "haiku"], input="hello\n\n/session\nmore\n/exit\n")
    assert result.exit_code == 0, result.output
    assert "echo: hello" in result.output and "echo: more" in result.output
    assert "sess-123" in result.output  # /session and the closing hint
    argv = fake_claude.argv()  # the last call: `more`
    assert argv[argv.index("--resume") + 1] == "sess-123"
    assert "--no-session-persistence" not in argv and argv[argv.index("--model") + 1] == "haiku"


def test_chat_new_starts_fresh_and_survives_errors(fake_claude: FakeClaude) -> None:
    fake_claude.respond(is_error=True, api_error_status=500, result="boom")
    result = runner.invoke(app, ["chat"], input="first\n/new\nsecond\n")
    assert result.exit_code == 0, result.output
    assert result.output.count("error [claude_error]") == 2
    argv = fake_claude.argv()
    assert "--resume" not in argv  # /new dropped the session; EOF ends the loop


def test_usage_command(fake_claude: FakeClaude) -> None:
    empty = runner.invoke(app, ["usage"])
    assert (
        empty.exit_code == 0 and "0 calls" in empty.output and "no calls recorded" in empty.output
    )
    assert runner.invoke(app, ["ask", "q", "-k", "ja"]).exit_code == 0
    assert runner.invoke(app, ["ask", "q", "-m", "opus", "-k", "none"]).exit_code == 0
    out = runner.invoke(app, ["usage", "--tail", "1"]).output
    assert "2 calls (2 ok, 0 errors)" in out and "out 10" in out and "skills: " in out
    assert "sonnet" in out and "opus" in out
    payload = json.loads(runner.invoke(app, ["usage", "--json", "--since", "all"]).output)
    assert payload["summary"]["calls"] == 2 and payload["summary"]["by_skill"]["ja"] == 1
    assert runner.invoke(app, ["usage", "--since", "soon"]).exit_code == 2
    assert runner.invoke(app, ["usage", "--path"]).output.strip().endswith("usage.jsonl")


def test_skills_commands(fake_claude: FakeClaude) -> None:
    out = runner.invoke(app, ["skills", "list"]).output
    assert "ja" in out and "zh" in out and "bundled" in out
    assert runner.invoke(app, ["skills", "use", "ja"]).exit_code == 0
    assert load().skill == "ja"
    assert runner.invoke(app, ["skills", "list"]).output.startswith("* ja")
    assert runner.invoke(app, ["skills", "use", "nope"]).exit_code == 1
    assert runner.invoke(app, ["skills", "use", "none"]).exit_code == 0 and load().skill is None
    show = runner.invoke(app, ["skills", "show", "zh"]).output
    assert "name: zh" in show and "pinyin" in show
    exported = runner.invoke(app, ["skills", "export", "ja"]).output.strip()
    assert exported.endswith("skills/ja.md")
    assert runner.invoke(app, ["skills", "path"]).output.strip().endswith("skills")


def test_ask_with_skill_and_component(fake_claude: FakeClaude) -> None:
    tree = {"component": {"type": "text", "markdown": "hello **there**"}}
    fake_claude.respond(structured_output=tree)
    result = runner.invoke(app, ["ask", "q", "-k", "ja", "-c"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == tree
    as_json = json.loads(runner.invoke(app, ["ask", "q", "-k", "ja", "-c", "--json"]).output)
    assert as_json["structured"] == tree and as_json["text"] == ""  # the tree is the answer
    result = runner.invoke(app, ["ask", "q", "-k", "ja", "-c", "--render", "html"])
    assert result.exit_code == 0 and result.output.strip() == (
        '<div class="bot-answer bot-answer-text"><p>hello <b>there</b></p></div>'
    )
    argv = fake_claude.argv()
    assert "--json-schema" in argv and argv[argv.index("--model") + 1] == "sonnet"


def test_config_schema_component_flag(fake_claude: FakeClaude) -> None:
    schema = json.loads(runner.invoke(app, ["config", "schema", "--component"]).output)
    assert "component" in schema["properties"]
