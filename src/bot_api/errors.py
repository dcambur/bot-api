from __future__ import annotations

from typing import Any

from .contract import AskError, ErrorCode, ErrorInfo


class BotApiError(Exception):
    """Raised by the runner; carries a stable :class:`ErrorCode` for programmatic callers."""

    def __init__(
        self, code: ErrorCode, message: str, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_result(self) -> AskError:
        return AskError(error=ErrorInfo(code=self.code, message=self.message, details=self.details))
