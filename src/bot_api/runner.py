"""Runs ``claude -p`` as a subprocess and maps its output onto the contract.

Design notes
- We deliberately do NOT use ``--bare``: it disables subscription (claude.ai) login.
  Isolation is achieved instead with ``--tools ""``, ``--max-turns 1``,
  ``--setting-sources ""``, ``--safe-mode`` (when the installed CLI has it; it cuts
  ~1.5 s of start-up) and a replaced ``--system-prompt``.
- The prompt is sent on stdin (no argv length limits, no shell quoting of OCR text).
- ``CLAUDE_CODE_EFFORT_LEVEL`` is stripped from the environment because it would
  override ``--effort``; thinking is disabled with ``MAX_THINKING_TOKENS=0``. API-key and
  third-party-provider variables are stripped as well so the child cannot silently leave
  the subscription login (``keep_auth_env = true`` passes them through).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import catalog, components, config, skillset, usage
from .contract import AskRequest, AskResponse, AskResult, Effort, ErrorCode, Usage
from .errors import BotApiError

_BIN_CANDIDATES = (
    "~/.npm-global/bin/claude",
    "~/.claude/local/claude",
    "~/.local/bin/claude",
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
)

#: Variables that would move the child off the subscription login. Stripped unless
#: ``keep_auth_env`` is set.
AUTH_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)

#: Request values for ``skill`` that mean "no skill, even if one is configured".
NO_SKILL = ("", "none")


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


def supports_safe_mode(claude: Path) -> bool:
    """Whether this ``claude`` accepts ``--safe-mode``.

    ``claude --help`` costs ~0.3 s, so the answer is cached per binary (path, mtime, size)
    in ``<config dir>/cli-cache.json``.
    """
    cache_path = config.config_path().parent / "cli-cache.json"
    try:
        st = claude.stat()
    except OSError:
        return False
    key = f"{claude}:{st.st_mtime_ns}:{st.st_size}"
    cache: dict[str, Any] = {}
    try:
        loaded = json.loads(cache_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            cache = loaded
    except (OSError, ValueError):
        pass
    if isinstance(cache.get(key), bool):
        return bool(cache[key])
    try:
        out = subprocess.run(
            [str(claude), "--help"], capture_output=True, text=True, encoding="utf-8", timeout=30
        )
        supported = "--safe-mode" in out.stdout
    except (OSError, subprocess.SubprocessError):
        supported = False
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({key: supported}), encoding="utf-8")
    except OSError:
        pass
    return supported


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
    component: bool = False
    persisted: bool = False
    safe_mode: bool = False
    keep_auth_env: bool = False
    usage_log: bool = True
    warnings: list[str] = field(default_factory=list)


def _first[T](*values: T | None) -> T | None:
    return next((v for v in values if v is not None), None)


def resolve_effective(request: AskRequest, settings: config.Settings) -> Effective:
    """Merge precedence: request > skill > settings."""
    skill: skillset.Skill | None = None
    name = None if request.skill in NO_SKILL else (request.skill or settings.skill)
    if name:
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
        component=request.component,
        persisted=bool(request.session_id or request.persist_session),
        safe_mode=settings.safe_mode,
        keep_auth_env=settings.keep_auth_env,
        usage_log=settings.usage_log,
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
    ]
    if eff.safe_mode:
        cmd.append("--safe-mode")
    cmd += ["--system-prompt", eff.system_prompt, "--model", eff.model]
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
    if not eff.keep_auth_env:
        for var in AUTH_ENV:
            env.pop(var, None)
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
    usage_ = payload.get("usage") or {}
    model_usage: dict[str, Any] = payload.get("modelUsage") or {}
    session_id = payload.get("session_id")
    return AskResponse(
        text=str(payload.get("result") or ""),
        structured=payload.get("structured_output"),
        model=next(iter(model_usage), eff.model),
        # Only a persisted session can be resumed; anything else would be a dangling ID.
        session_id=str(session_id) if eff.persisted and session_id else None,
        usage=Usage(
            input_tokens=usage_.get("input_tokens", 0),
            output_tokens=usage_.get("output_tokens", 0),
            cache_read_input_tokens=usage_.get("cache_read_input_tokens", 0),
            cache_creation_input_tokens=usage_.get("cache_creation_input_tokens", 0),
            thinking_tokens=(usage_.get("output_tokens_details") or {}).get("thinking_tokens", 0),
        ),
        cost_usd=float(payload.get("total_cost_usd") or 0.0),
        duration_ms=int(payload.get("duration_ms") or 0),
        stop_reason=payload.get("stop_reason"),
        skill=eff.skill,
        warnings=list(eff.warnings),
    )


def validate_component(response: AskResponse) -> components.Answer | None:
    """Check a component-mode answer; a bad tree becomes a warning, not a failure.

    When the tree is good, ``text`` (the same JSON as a string) is cleared: the tree is the
    answer. When it is bad, the raw text stays so a host has something to show.
    """
    if not isinstance(response.structured, dict):
        response.warnings.append("component mode: no structured output returned")
        return None
    try:
        answer = components.Answer.model_validate(response.structured)
    except ValueError as exc:
        response.warnings.append(f"component tree failed validation: {exc}")
        return None
    response.text = ""
    return answer


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


# --- orchestration --------------------------------------------------------------------


def _prepare(
    request: AskRequest, settings: config.Settings | None, *, stream: bool
) -> tuple[Effective, list[str]]:
    settings = settings or config.load()
    eff = resolve_effective(request, settings)
    claude = find_claude(settings.claude_bin)
    if eff.safe_mode and not supports_safe_mode(claude):
        eff.safe_mode = False
    return eff, build_command(claude, request, eff, stream=stream)


def _ledger_base(request: AskRequest, eff: Effective) -> dict[str, Any]:
    return {
        "ts": usage.now_iso(),
        "model": eff.model,
        "skill": eff.skill,
        "component": eff.component,
        "prompt_chars": len(request.prompt),
    }


def _finish(response: AskResponse, request: AskRequest, eff: Effective) -> AskResponse:
    if request.component:
        validate_component(response)
    if eff.usage_log:
        usage.append(
            _ledger_base(request, eff)
            | {
                "ok": True,
                "model": response.model,
                "session_id": response.session_id,
                "duration_ms": response.duration_ms,
                "cost_usd": response.cost_usd,
                "stop_reason": response.stop_reason,
            }
            | response.usage.model_dump()
        )
    return response


def _log_failure(request: AskRequest, eff: Effective, exc: BotApiError) -> None:
    if eff.usage_log:
        usage.append(_ledger_base(request, eff) | {"ok": False, "error": exc.code.value})


def _run_once(cmd: list[str], request: AskRequest, eff: Effective) -> AskResponse:
    try:
        proc = subprocess.run(
            cmd,
            input=request.prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
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
    return parse_result(payload, eff)


def ask(request: AskRequest, settings: config.Settings | None = None) -> AskResponse:
    """Send one prompt; return the answer. Raises :class:`BotApiError`."""
    eff, cmd = _prepare(request, settings, stream=False)
    try:
        response = _run_once(cmd, request, eff)
    except BotApiError as exc:
        _log_failure(request, eff, exc)
        raise
    return _finish(response, request, eff)


def ask_result(request: AskRequest, settings: config.Settings | None = None) -> AskResult:
    """Like :func:`ask` but never raises: errors become :class:`AskError`."""
    try:
        return ask(request, settings)
    except BotApiError as exc:
        return exc.to_result()


def _stream_lines(proc: subprocess.Popen[str], prompt: str) -> Iterator[dict[str, Any]]:
    assert proc.stdin is not None and proc.stdout is not None
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
    except BrokenPipeError:  # child died (or was killed) before reading the prompt
        return
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


def _run_stream(
    cmd: list[str], request: AskRequest, eff: Effective, on_text: Callable[[str], None]
) -> AskResponse:
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
        # The deadline must cover the whole stream, not just the wait after it: a child
        # that stalls mid-answer would otherwise block the read loop for ever.
        timed_out = threading.Event()

        def _expire() -> None:
            timed_out.set()
            proc.kill()

        watchdog = threading.Timer(eff.timeout_s, _expire)
        watchdog.daemon = True
        watchdog.start()
        try:
            for event in _stream_lines(proc, request.prompt):
                if event.get("type") == "stream_event":
                    delta = (event.get("event") or {}).get("delta") or {}
                    if delta.get("type") == "text_delta":
                        on_text(str(delta.get("text", "")))
                elif event.get("type") == "result":
                    result = event
            proc.wait()
        finally:
            watchdog.cancel()
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        if timed_out.is_set():
            raise BotApiError(ErrorCode.timeout, f"claude did not answer within {eff.timeout_s:g}s")
        if result is None:
            stderr.seek(0)
            raise BotApiError(
                ErrorCode.claude_error if proc.returncode else ErrorCode.bad_output,
                f"claude exited with {proc.returncode} without a result message",
                {"stderr": stderr.read().strip()[-2000:]},
            )
    return parse_result(result, eff)


def stream_ask(
    request: AskRequest,
    on_text: Callable[[str], None],
    settings: config.Settings | None = None,
) -> AskResponse:
    """Like :func:`ask`, but calls ``on_text`` with each text chunk as it arrives."""
    eff, cmd = _prepare(request, settings, stream=True)
    try:
        response = _run_stream(cmd, request, eff, on_text)
    except BotApiError as exc:
        _log_failure(request, eff, exc)
        raise
    return _finish(response, request, eff)
