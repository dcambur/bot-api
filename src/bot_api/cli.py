"""``bot`` command-line interface."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import ValidationError

from . import catalog, components, config, skillset
from .contract import (
    AskError,
    AskRequest,
    AskResponse,
    Effort,
    ErrorCode,
    ErrorInfo,
    ThinkingConfig,
)
from .errors import BotApiError
from .runner import ask as run_ask
from .runner import ask_result, find_claude, stream_ask, validate_component

app = typer.Typer(
    help="Chat with Claude from the terminal via the Claude Code CLI (uses your subscription).",
    no_args_is_help=True,
    add_completion=False,
)
models_app = typer.Typer(help="List and select models.", no_args_is_help=True)
thinking_app = typer.Typer(help="Configure extended thinking and effort.", no_args_is_help=True)
config_app = typer.Typer(help="Inspect and edit settings.", no_args_is_help=True)
skills_app = typer.Typer(help="Prompt presets (ja, zh, ...).", no_args_is_help=True)
app.add_typer(models_app, name="models")
app.add_typer(thinking_app, name="thinking")
app.add_typer(config_app, name="config")
app.add_typer(skills_app, name="skills")

err = typer.echo  # alias for readability; used with err=True


def _dump(model: Any) -> str:
    return json.dumps(model.model_dump(mode="json"), ensure_ascii=False, indent=2)


def _fail(exc: BotApiError, *, as_json: bool) -> None:
    if as_json:
        typer.echo(_dump(exc.to_result()))
    else:
        typer.echo(f"error [{exc.code.value}]: {exc.message}", err=True)
        if exc.details and exc.details.get("stderr"):
            typer.echo(exc.details["stderr"], err=True)
    raise typer.Exit(code=1)


def _read_stdin() -> str | None:
    if sys.stdin.isatty():
        return None
    data = sys.stdin.read()
    return data if data.strip() else None


@app.command()
def ask(
    prompt: Annotated[str | None, typer.Argument(help="Message. Piped stdin is appended.")] = None,
    model: Annotated[str | None, typer.Option("--model", "-m", help="Alias or full ID.")] = None,
    skill: Annotated[
        str | None, typer.Option("--skill", "-k", help="Skill name (see `bot skills list`).")
    ] = None,
    component: Annotated[
        bool, typer.Option("--component", "-c", help="Return a typed UI component tree.")
    ] = False,
    render: Annotated[
        str, typer.Option(help="With --component: 'json' (default) or 'html'.")
    ] = "json",
    effort: Annotated[Effort | None, typer.Option(help="Thinking effort level.")] = None,
    thinking: Annotated[
        bool | None, typer.Option("--thinking/--no-thinking", help="Toggle extended thinking.")
    ] = None,
    system: Annotated[str | None, typer.Option("--system", "-s", help="System prompt.")] = None,
    schema: Annotated[
        Path | None, typer.Option(help="JSON Schema file for structured output.")
    ] = None,
    session: Annotated[str | None, typer.Option(help="Resume a session_id.")] = None,
    persist: Annotated[bool, typer.Option(help="Keep the session so it can be resumed.")] = False,
    timeout: Annotated[float | None, typer.Option(help="Seconds to wait.")] = None,
    stream: Annotated[bool, typer.Option(help="Print tokens as they arrive.")] = False,
    json_out: Annotated[bool, typer.Option("--json", help="Emit the AskResult contract.")] = False,
    request: Annotated[
        bool, typer.Option("--request", help="Read an AskRequest JSON from stdin (implies --json).")
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Usage/cost to stderr.")] = False,
) -> None:
    """Send a prompt and print the answer."""
    if request:
        raw = _read_stdin() or ""
        try:
            req = AskRequest.model_validate_json(raw)
        except ValidationError as exc:
            info = ErrorInfo(code=ErrorCode.invalid_request, message=str(exc.errors()[0]["msg"]))
            typer.echo(_dump(AskError(error=info)))
            raise typer.Exit(code=1) from exc
        result = ask_result(req)
        typer.echo(_dump(result))
        raise typer.Exit(code=0 if result.ok else 1)

    piped = _read_stdin()
    text = f"{prompt}\n\n{piped}" if prompt and piped else (prompt or piped or "")
    if not text.strip():
        typer.echo("error: no prompt given (argument or stdin)", err=True)
        raise typer.Exit(code=2)

    thinking_cfg = (
        ThinkingConfig(enabled=thinking, effort=effort)
        if thinking is not None or effort is not None
        else None
    )
    try:
        req = AskRequest(
            prompt=text,
            model=model,
            skill=skill,
            component=component,
            system_prompt=system,
            thinking=thinking_cfg,
            session_id=session,
            persist_session=persist,
            output_schema=json.loads(schema.read_text(encoding="utf-8")) if schema else None,
            timeout_s=timeout,
        )
        if stream and not json_out:
            response = stream_ask(req, lambda chunk: typer.echo(chunk, nl=False))
            typer.echo("")
        else:
            response = run_ask(req)
    except BotApiError as exc:
        _fail(exc, as_json=json_out)
        return

    if json_out:
        typer.echo(_dump(response))
    elif component:
        answer = validate_component(response)
        if render == "html" and answer is not None:
            typer.echo(components.render_html(answer))
        else:
            typer.echo(json.dumps(response.structured, ensure_ascii=False, indent=2))
    elif not stream:
        typer.echo(response.text)
        if response.structured is not None:
            typer.echo(json.dumps(response.structured, ensure_ascii=False, indent=2))
    _footer(response, verbose)


def _footer(response: AskResponse, verbose: bool) -> None:
    for warning in response.warnings:
        typer.echo(f"warning: {warning}", err=True)
    if verbose:
        u = response.usage
        skill = f" skill={response.skill}" if response.skill else ""
        typer.echo(
            f"[{response.model}{skill}] in={u.input_tokens} out={u.output_tokens} "
            f"thinking={u.thinking_tokens} cost=${response.cost_usd:.4f} "
            f"time={response.duration_ms}ms session={response.session_id}",
            err=True,
        )


# --- models -----------------------------------------------------------------------------


@models_app.command("list")
def models_list(
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show the model catalog (aliases, effort levels, thinking toggle)."""
    settings = config.load()
    current = catalog.resolve(settings.model)
    if json_out:
        rows = [
            {
                "id": spec.id,
                "label": spec.label,
                "aliases": list(spec.aliases),
                "efforts": [e.value for e in spec.efforts],
                "thinking_switchable": spec.thinking_switchable,
                "current": spec is current,
            }
            for spec in catalog.CATALOG
        ]
        typer.echo(json.dumps(rows, indent=2))
        return
    typer.echo(f"{'':2}{'ID':22} {'ALIASES':12} {'EFFORT':28} THINKING")
    for spec in catalog.CATALOG:
        mark = "*" if spec is current else " "
        efforts = ",".join(e.value for e in spec.efforts) or "-"
        toggle = "on/off" if spec.thinking_switchable else "always on"
        typer.echo(f"{mark:2}{spec.id:22} {','.join(spec.aliases):12} {efforts:28} {toggle}")
    if current is None:
        typer.echo(f"\ncurrent: {settings.model} (custom, not in catalog)")


@models_app.command("set")
def models_set(
    name: Annotated[str, typer.Argument(help="Alias (sonnet, opus, ...) or full ID.")],
    allow_unknown: Annotated[
        bool, typer.Option("--allow-unknown", help="Accept an ID not in the catalog.")
    ] = False,
) -> None:
    """Set the default model."""
    settings = config.load()
    spec = catalog.resolve(name)
    if spec is None and not allow_unknown:
        typer.echo(
            f"error: unknown model {name!r}; see `bot models list` or pass --allow-unknown",
            err=True,
        )
        raise typer.Exit(code=1)
    if (
        spec is not None
        and settings.effort is not None
        and not spec.supports_effort(settings.effort)
    ):
        typer.echo(
            f"note: {spec.id} does not support effort {settings.effort.value!r}; resetting effort "
            "to the model default",
            err=True,
        )
        settings.effort = None
    settings.model = name
    config.save(settings)
    typer.echo(f"model: {name}" + (f" ({spec.id})" if spec and spec.id != name else ""))


@models_app.command("current")
def models_current() -> None:
    """Print the configured model."""
    settings = config.load()
    spec = catalog.resolve(settings.model)
    typer.echo(settings.model + (f" -> {spec.id}" if spec and spec.id != settings.model else ""))


# --- thinking ---------------------------------------------------------------------------


def _set_thinking(enabled: bool) -> None:
    settings = config.load()
    settings.thinking_enabled = enabled
    config.save(settings)
    spec = catalog.resolve(settings.model)
    if not enabled and spec is not None and not spec.thinking_switchable:
        typer.echo(f"note: thinking cannot be turned off on {spec.id}; saved anyway", err=True)
    typer.echo(f"thinking: {'on' if enabled else 'off'}")


@thinking_app.command("on")
def thinking_on() -> None:
    """Enable extended thinking."""
    _set_thinking(True)


@thinking_app.command("off")
def thinking_off() -> None:
    """Disable extended thinking (no effect on Fable models)."""
    _set_thinking(False)


@thinking_app.command("effort")
def thinking_effort(
    level: Annotated[str, typer.Argument(help="low|medium|high|xhigh|max|default")],
) -> None:
    """Set the effort level, or `default` to use the model's own default."""
    settings = config.load()
    if level == "default":
        settings.effort = None
    else:
        try:
            effort = Effort(level)
        except ValueError as exc:
            typer.echo(f"error: invalid effort {level!r}", err=True)
            raise typer.Exit(code=1) from exc
        spec = catalog.resolve(settings.model)
        if spec is not None and not spec.supports_effort(effort):
            supported = ", ".join(e.value for e in spec.efforts) or "none"
            typer.echo(f"error: {spec.id} supports: {supported}", err=True)
            raise typer.Exit(code=1)
        settings.effort = effort
    config.save(settings)
    typer.echo(f"effort: {settings.effort.value if settings.effort else 'default'}")


@thinking_app.command("show")
def thinking_show() -> None:
    """Print the thinking configuration."""
    settings = config.load()
    typer.echo(f"thinking: {'on' if settings.thinking_enabled else 'off'}")
    typer.echo(f"effort: {settings.effort.value if settings.effort else 'default'}")


# --- config -----------------------------------------------------------------------------


@config_app.command("show")
def config_show(json_out: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Print all settings."""
    settings = config.load()
    if json_out:
        typer.echo(_dump(settings))
        return
    for key, value in settings.model_dump(mode="json").items():
        typer.echo(f"{key} = {json.dumps(value, ensure_ascii=False)}")


@config_app.command("set")
def config_set(key: str, value: str) -> None:
    """Set one setting, e.g. `bot config set system_prompt "You are a Japanese tutor."`."""
    try:
        settings = config.set_value(config.load(), key, value)
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    config.save(settings)
    typer.echo(f"{key} = {json.dumps(getattr(settings, key), ensure_ascii=False)}")


@config_app.command("path")
def config_path_cmd() -> None:
    """Print the config file location."""
    typer.echo(str(config.config_path()))


@config_app.command("schema")
def config_schema(
    component_only: Annotated[
        bool, typer.Option("--component", help="Print only the UI component schema.")
    ] = False,
) -> None:
    """Print the JSON Schema of the request/response (and component) contract."""
    if component_only:
        typer.echo(json.dumps(components.schema(), indent=2))
        return
    typer.echo(
        json.dumps(
            {
                "request": AskRequest.model_json_schema(),
                "response": AskResponse.model_json_schema(),
                "error": AskError.model_json_schema(),
                "component": components.schema(),
            },
            indent=2,
        )
    )


# --- skills -----------------------------------------------------------------------------


@skills_app.command("list")
def skills_list(json_out: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Show bundled and user skills."""
    settings = config.load()
    skills = skillset.load_all()
    if json_out:
        typer.echo(json.dumps([s.model_dump(mode="json") for s in skills.values()], indent=2))
        return
    for skill in skills.values():
        mark = "*" if skill.name == settings.skill else " "
        typer.echo(f"{mark} {skill.name:8} {skill.source:8} {skill.description}")
    if not skills:
        typer.echo("no skills found")


@skills_app.command("show")
def skills_show(name: str) -> None:
    """Print a skill's defaults and prompt."""
    try:
        skill = skillset.resolve(name)
    except skillset.SkillError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    meta = skill.model_dump(mode="json", exclude={"prompt"}, exclude_none=True)
    for key, value in meta.items():
        typer.echo(f"{key}: {value}")
    typer.echo("---")
    typer.echo(skill.prompt)


@skills_app.command("use")
def skills_use(
    name: Annotated[str, typer.Argument(help="Skill name, or 'none' to clear.")],
) -> None:
    """Make a skill the default for `bot ask`."""
    settings = config.load()
    if name == "none":
        settings.skill = None
    else:
        try:
            skillset.resolve(name)
        except skillset.SkillError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        settings.skill = name
    config.save(settings)
    typer.echo(f"skill: {settings.skill or 'none'}")


@skills_app.command("export")
def skills_export(name: str) -> None:
    """Copy a bundled skill into the user skills folder for editing."""
    try:
        path = skillset.export(name)
    except skillset.SkillError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(str(path))


@skills_app.command("path")
def skills_path() -> None:
    """Print the user skills folder."""
    typer.echo(str(skillset.user_dir()))


# --- serve ------------------------------------------------------------------------------


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Bind address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="TCP port.")] = 7788,
    cors: Annotated[str, typer.Option(help="Access-Control-Allow-Origin value.")] = "*",
) -> None:
    """Run a local HTTP endpoint (POST /ask, same contract) plus a playground at /."""
    from .server import run

    typer.echo(f"bot-api listening on http://{host}:{port}/  (Ctrl+C to stop)", err=True)
    run(host=host, port=port, cors=cors)


# --- doctor -----------------------------------------------------------------------------


@app.command()
def doctor() -> None:
    """Check that the Claude Code CLI is installed, authenticated, and the config is sane."""
    settings = config.load()
    ok = True
    typer.echo(f"config: {config.config_path()}")
    try:
        claude = find_claude(settings.claude_bin)
    except BotApiError as exc:
        typer.echo(f"claude: MISSING - {exc.message}")
        raise typer.Exit(code=1) from exc
    version = subprocess.run([str(claude), "--version"], capture_output=True, text=True)
    typer.echo(f"claude: {claude} ({version.stdout.strip() or version.stderr.strip()})")
    auth = subprocess.run([str(claude), "auth", "status"], capture_output=True, text=True)
    try:
        status = json.loads(auth.stdout)
    except json.JSONDecodeError:
        status = {}
    if status.get("loggedIn"):
        typer.echo(
            f"auth: logged in as {status.get('email', '?')} "
            f"({status.get('subscriptionType') or status.get('authMethod', '?')})"
        )
    else:
        ok = False
        typer.echo("auth: NOT logged in - run `claude auth login`")
    spec = catalog.resolve(settings.model)
    typer.echo(f"model: {settings.model}" + (f" -> {spec.id}" if spec else " (custom)"))
    typer.echo(
        f"thinking: {'on' if settings.thinking_enabled else 'off'}, "
        f"effort: {settings.effort.value if settings.effort else 'default'}"
    )
    raise typer.Exit(code=0 if ok else 1)
