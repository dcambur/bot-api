import pytest
from pydantic import ValidationError

from bot_api import AskError, AskRequest, AskResponse, Effort, ThinkingConfig


def test_request_defaults_and_roundtrip() -> None:
    req = AskRequest(prompt="hi")
    assert req.contract_version == "1"
    assert req.model is None and req.thinking is None
    again = AskRequest.model_validate_json(req.model_dump_json())
    assert again == req


def test_request_rejects_unknown_fields_and_empty_prompt() -> None:
    with pytest.raises(ValidationError):
        AskRequest.model_validate({"prompt": "x", "bogus": 1})
    with pytest.raises(ValidationError):
        AskRequest(prompt="")
    with pytest.raises(ValidationError):
        AskRequest(prompt="x", thinking=ThinkingConfig(effort="turbo"))  # type: ignore[arg-type]


def test_response_and_error_discriminate_on_ok() -> None:
    ok = AskResponse(text="t", model="m", session_id="s")
    assert ok.ok is True and ok.usage.input_tokens == 0
    err = AskError.model_validate({"ok": False, "error": {"code": "timeout", "message": "slow"}})
    assert err.ok is False and err.error.code == "timeout"


def test_effort_values() -> None:
    assert [e.value for e in Effort] == ["low", "medium", "high", "xhigh", "max"]
