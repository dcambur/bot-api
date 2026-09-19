"""bot-api: chat with Claude from the terminal or from other programs.

The public surface is intentionally small:

- :func:`ask` / :func:`stream_ask` — send one prompt, get one answer.
- :class:`AskRequest` / :class:`AskResponse` / :class:`AskError` — the message contract.
- :class:`BotApiError` — raised by :func:`ask`; :func:`ask_result` converts it to :class:`AskError`.
"""

from .contract import (
    CONTRACT_VERSION,
    AskError,
    AskRequest,
    AskResponse,
    AskResult,
    Effort,
    ErrorCode,
    ThinkingConfig,
    Usage,
)
from .errors import BotApiError
from .runner import ask, ask_result, stream_ask

__all__ = [
    "CONTRACT_VERSION",
    "AskError",
    "AskRequest",
    "AskResponse",
    "AskResult",
    "BotApiError",
    "Effort",
    "ErrorCode",
    "ThinkingConfig",
    "Usage",
    "ask",
    "ask_result",
    "stream_ask",
]
