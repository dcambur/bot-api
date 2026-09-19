"""Runs ``claude -p`` as a subprocess and maps its output onto the contract.

Design notes
- We deliberately do NOT use ``--bare``: it disables subscription (claude.ai) login.
  Isolation is achieved instead with ``--tools ""``, ``--max-turns 1``,
  ``--setting-sources ""`` and a replaced ``--system-prompt``.
- The prompt is sent on stdin (no argv length limits, no shell quoting of OCR text).
- ``CLAUDE_CODE_EFFORT_LEVEL`` is stripped from the environment because it would
  override ``--effort``; thinking is disabled with ``MAX_THINKING_TOKENS=0``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import catalog, components, config, skillset
from .contract import AskRequest, AskResponse, AskResult, Effort, ErrorCode, Usage
from .errors import BotApiError

_BIN_CANDIDATES = (
    "~/.npm-global/bin/claude",
    "~/.claude/local/claude",
    "~/.local/bin/claude",
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
)


def find_claude(explicit: str | None = None) -> Path:
    """Locate the ``claude`` executable: env > settings > PATH > well-known locations."""
    for candidate in (os.environ.get("BOT_API_CLAUDE_BIN"), explicit):
        if candidate:
            path = Path(candidate).expanduser()
            if path.is_file() and os.access(path, os.X_OK):
                return path
            raise BotApiError(ErrorCode.claude_not_found, f"claude executable not usable: {path}")
    if found := shutil.which("claude"):
        return Path(found)
    for candidate in _BIN_CANDIDATES:
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return path
    raise BotApiError(
        ErrorCode.claude_not_found,
        "Claude Code CLI not found. Install: npm install -g @anthropic-ai/claude-code, "
        "then run `claude auth login`, or set BOT_API_CLAUDE_BIN / claude_bin.",
    )


@dataclass(slots=True)
class Effective:
    """Request merged with settings; what actually gets sent."""

    model: str
    effort: Effort | None
    thinking_enabled: bool
    system_prompt: str
    timeout_s: float
    skill: str | None = None
    output_schema: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)


def _first[T](*values: T | None) -> T | None:
    return next((v for v in values if v is not None), None)


def resolve_effective(request: AskRequest, settings: config.Settings) -> Effective:
    """Merge precedence: request > skill > settings."""
    skill: skillset.Skill | None = None
    if name := request.skill or settings.skill:
        try:
            skill = skillset.resolve(name)
        except skillset.SkillError as exc:
            raise BotApiError(ErrorCode.invalid_request, str(exc)) from exc
    thinking = request.thinking
    system_prompt = _first(
        request.system_prompt, skill.prompt if skill else None, settings.system_prompt
    )
    assert system_prompt is not None
    eff = Effective(
        model=_first(request.model, skill.model if skill else None, settings.model) or "",
        effort=_first(
            thinking.effort if thinking else None, skill.effort if skill else None, settings.effort
        ),
        thinking_enabled=_first(
            thinking.enabled if thinking else None,
            skill.thinking_enabled if skill else None,
            settings.thinking_enabled,
        )
        or False,
        system_prompt=system_prompt,
        timeout_s=request.timeout_s or settings.timeout_s,
        skill=skill.name if skill else None,
        output_schema=request.output_schema,
    )
    if request.component:
        eff.system_prompt = f"{eff.system_prompt}\n\n{components.COMPONENT_RULES}"
        if eff.output_schema is None:
            eff.output_schema = components.schema()
    spec = catalog.resolve(eff.model)
    if spec is None:
        eff.warnings.append(
            f"model {eff.model!r} is not in the catalog; passed through unvalidated"
        )
        return eff
    if eff.effort is not None and not spec.supports_effort(eff.effort):
        supported = ", ".join(e.value for e in spec.efforts) or "none"
        raise BotApiError(
            ErrorCode.unsupported_effort,
            f"{spec.id} does not support effort {eff.effort.value!r} (supported: {supported})",
        )
    if not eff.thinking_enabled and not spec.thinking_switchable:
        eff.warnings.append(f"thinking cannot be turned off on {spec.id}; it stays on")
    return eff


def build_command(claude: Path, request: AskRequest, eff: Effective, *, stream: bool) -> list[str]:
    cmd = [
        str(claude),
        "-p",
        "--output-format",
        "stream-json" if stream else "json",
        "--tools",
        "",
        "--max-turns",
        "1",
        "--setting-sources",
        "",
        "--system-prompt",
        eff.system_prompt,
        "--model",
        eff.model,
    ]
    if stream:
        cmd += ["--verbose", "--include-partial-messages"]
    if eff.effort is not None:
        cmd += ["--effort", eff.effort.value]
    if request.session_id:
        cmd += ["--resume", request.session_id]
    elif not request.persist_session:
        cmd.append("--no-session-persistence")
    if eff.output_schema is not None:
        cmd += ["--json-schema", json.dumps(eff.output_schema)]
    return cmd


def build_env(eff: Effective) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("CLAUDE_CODE_EFFORT_LEVEL", None)  # would silently override --effort
    env.pop("ANTHROPIC_MODEL", None)
    if eff.thinking_enabled:
        env.pop("MAX_THINKING_TOKENS", None)
    else:
        env["MAX_THINKING_TOKENS"] = "0"
    return env


def _workdir() -> Path:
    """Stable cwd so claude's per-project state never lands in the caller's directory."""
    path = config.config_path().parent / "workdir"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _map_error(payload: dict[str, Any]) -> BotApiError:
    status = payload.get("api_error_status")
    message = str(payload.get("result") or "claude reported an error")
    lowered = message.lower()
    details = {"api_error_status": status, "terminal_reason": payload.get("terminal_reason")}
    if status in (401, 403) or "log in" in lowered or "login" in lowered or "authent" in lowered:
        return BotApiError(ErrorCode.not_authenticated, message, details)
    if status == 404 and "model" in lowered:
        return BotApiError(ErrorCode.invalid_model, message, details)
    return BotApiError(ErrorCode.claude_error, message, details)


def parse_result(payload: dict[str, Any], eff: Effective) -> AskResponse:
    if payload.get("type") != "result":
        raise BotApiError(ErrorCode.bad_output, "claude did not return a result message")
    if payload.get("is_error"):
        raise _map_error(payload)
    usage = payload.get("usage") or {}
    model_usage: dict[str, Any] = payload.get("modelUsage") or {}
    return AskResponse(
        text=str(payload.get("result") or ""),
        structured=payload.get("structured_output"),
        model=next(iter(model_usage), eff.model),
        session_id=str(payload.get("session_id", "")),
        usage=Usage(
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cache_read_input_tokens=usage.get("cache_read_input_tokens", 0),
            cache_creation_input_tokens=usage.get("cache_creation_input_tokens", 0),
            thinking_tokens=(usage.get("output_tokens_details") or {}).get("thinking_tokens", 0),
        ),
        cost_usd=float(payload.get("total_cost_usd") or 0.0),
        duration_ms=int(payload.get("duration_ms") or 0),
        stop_reason=payload.get("stop_reason"),
        skill=eff.skill,
        warnings=list(eff.warnings),
    )


def validate_component(response: AskResponse) -> components.Answer | None:
    """Check a component-mode answer; a bad tree becomes a warning, not a failure."""
    if not isinstance(response.structured, dict):
        response.warnings.append("component mode: no structured output returned")
        return None
    try:
        return components.Answer.model_validate(response.structured)
    except ValueError as exc:
        response.warnings.append(f"component tree failed validation: {exc}")
        return None


def _extract_json(stdout: str) -> dict[str, Any] | None:
    """The result is normally the whole stdout; tolerate stray log lines before it."""
    try:
        data = json.loads(stdout)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                return data
    return None


def ask(request: AskRequest, settings: config.Settings | None = None) -> AskResponse:
    """Send one prompt; return the answer. Raises :class:`BotApiError`."""
    settings = settings or config.load()
    eff = resolve_effective(request, settings)
    claude = find_claude(settings.claude_bin)
    cmd = build_command(claude, request, eff, stream=False)
    try:
        proc = subprocess.run(
            cmd,
            input=request.prompt,
            capture_output=True,
            text=True,
            env=build_env(eff),
            cwd=_workdir(),
            timeout=eff.timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        raise BotApiError(
            ErrorCode.timeout, f"claude did not answer within {eff.timeout_s:g}s"
        ) from exc
    payload = _extract_json(proc.stdout)
    if payload is None:
        code = ErrorCode.claude_error if proc.returncode else ErrorCode.bad_output
        raise BotApiError(
            code,
            f"claude exited with {proc.returncode} and no JSON result",
            {"stderr": proc.stderr.strip()[-2000:], "stdout": proc.stdout.strip()[-2000:]},
        )
    response = parse_result(payload, eff)
    if request.component:
        validate_component(response)
    return response


def ask_result(request: AskRequest, settings: config.Settings | None = None) -> AskResult:
    """Like :func:`ask` but never raises: errors become :class:`AskError`."""
    try:
        return ask(request, settings)
    except BotApiError as exc:
        return exc.to_result()


def _stream_lines(proc: subprocess.Popen[str], prompt: str) -> Iterator[dict[str, Any]]:
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write(prompt)
    proc.stdin.close()
    for line in proc.stdout:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event


def stream_ask(
    request: AskRequest,
    on_text: Callable[[str], None],
    settings: config.Settings | None = None,
) -> AskResponse:
    """Like :func:`ask`, but calls ``on_text`` with each text chunk as it arrives."""
    settings = settings or config.load()
    eff = resolve_effective(request, settings)
    claude = find_claude(settings.claude_bin)
    cmd = build_command(claude, request, eff, stream=True)
    result: dict[str, Any] | None = None
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            text=True,
            encoding="utf-8",
            env=build_env(eff),
            cwd=_workdir(),
        )
        try:
            for event in _stream_lines(proc, request.prompt):
                if event.get("type") == "stream_event":
                    delta = (event.get("event") or {}).get("delta") or {}
                    if delta.get("type") == "text_delta":
                        on_text(str(delta.get("text", "")))
                elif event.get("type") == "result":
                    result = event
            proc.wait(timeout=eff.timeout_s)
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            raise BotApiError(
                ErrorCode.timeout, f"claude did not answer within {eff.timeout_s:g}s"
            ) from exc
        finally:
            if proc.poll() is None:
                proc.kill()
        if result is None:
            stderr.seek(0)
            raise BotApiError(
                ErrorCode.claude_error if proc.returncode else ErrorCode.bad_output,
                f"claude exited with {proc.returncode} without a result message",
                {"stderr": stderr.read().strip()[-2000:]},
            )
    response = parse_result(result, eff)
    if request.component:
        validate_component(response)
    return response
