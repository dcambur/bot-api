"""The message contract shared by the CLI, the Python API, and external callers.

Every field here is stable within a ``contract_version``. Other services (an OCR
pipeline, an editor plugin, ...) should build an :class:`AskRequest`, send it, and
consume an :class:`AskResult`. ``bot config schema`` prints the JSON Schema for both.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

CONTRACT_VERSION: Final = "1"


class Effort(StrEnum):
    """Effort level passed to ``claude --effort``. Availability depends on the model."""

    low = "low"
    medium = "medium"
    high = "high"
    xhigh = "xhigh"
    max = "max"


class ErrorCode(StrEnum):
    invalid_request = "invalid_request"
    claude_not_found = "claude_not_found"
    not_authenticated = "not_authenticated"
    invalid_model = "invalid_model"
    unsupported_effort = "unsupported_effort"
    timeout = "timeout"
    claude_error = "claude_error"
    bad_output = "bad_output"
    busy = "busy"


class ThinkingConfig(BaseModel):
    """Per-request thinking override. ``None`` fields inherit the configured default."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = Field(
        default=None,
        description="Turn extended thinking on/off. Has no effect on Fable models.",
    )
    effort: Effort | None = Field(default=None, description="Effort level for this request.")


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["1"] = CONTRACT_VERSION
    prompt: str = Field(min_length=1, description="The user message.")
    system_prompt: str | None = Field(
        default=None, description="Replaces the configured system prompt for this request."
    )
    model: str | None = Field(
        default=None, description="Model alias (sonnet, opus, ...) or full ID. Default: configured."
    )
    skill: str | None = Field(
        default=None,
        description="Skill name (bot skills list); 'none' skips the configured default skill. "
        "Default: configured skill, if any.",
    )
    component: bool = Field(
        default=False,
        description="Ask for a typed UI component tree (see `bot config schema --component`).",
    )
    thinking: ThinkingConfig | None = None
    session_id: str | None = Field(
        default=None,
        description="Resume a previous exchange (AskResponse.session_id). Implies persistence.",
    )
    persist_session: bool = Field(
        default=False,
        description="Keep the session on disk so session_id can be resumed later.",
    )
    output_schema: dict[str, Any] | None = Field(
        default=None,
        description="JSON Schema; AskResponse.structured holds the validated object. "
        "Overrides the component schema.",
    )
    timeout_s: float | None = Field(default=None, gt=0, description="Wall-clock limit.")


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    thinking_tokens: int = 0


class AskResponse(BaseModel):
    contract_version: Literal["1"] = CONTRACT_VERSION
    ok: Literal[True] = True
    text: str
    structured: dict[str, Any] | list[Any] | None = None
    model: str
    session_id: str | None = Field(
        default=None, description="Set only when the session was persisted and can be resumed."
    )
    usage: Usage = Field(default_factory=Usage)
    cost_usd: float = 0.0
    duration_ms: int = 0
    stop_reason: str | None = None
    skill: str | None = None
    warnings: list[str] = Field(default_factory=list)


class ErrorInfo(BaseModel):
    code: ErrorCode
    message: str
    details: dict[str, Any] | None = None


class AskError(BaseModel):
    contract_version: Literal["1"] = CONTRACT_VERSION
    ok: Literal[False] = False
    error: ErrorInfo


AskResult = AskResponse | AskError
