"""Known models and what each supports.

The Claude Code CLI has no command that lists models, so this catalog is maintained by
hand (source: code.claude.com/docs/en/model-config). Unknown IDs are still accepted by
the runner and passed through to ``claude --model`` unvalidated.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .contract import Effort

ALL_EFFORTS = (Effort.low, Effort.medium, Effort.high, Effort.xhigh, Effort.max)
NO_XHIGH = (Effort.low, Effort.medium, Effort.high, Effort.max)


@dataclass(frozen=True, slots=True)
class ModelSpec:
    id: str
    label: str
    efforts: tuple[Effort, ...]
    thinking_switchable: bool = True
    aliases: tuple[str, ...] = ()

    def supports_effort(self, effort: Effort) -> bool:
        return effort in self.efforts


CATALOG: tuple[ModelSpec, ...] = (
    ModelSpec("claude-fable-5-1", "Claude Fable 5.1", ALL_EFFORTS, False, ("fable", "best")),
    ModelSpec("claude-fable-5", "Claude Fable 5", ALL_EFFORTS, False),
    ModelSpec("claude-opus-5", "Claude Opus 5", ALL_EFFORTS, True, ("opus",)),
    ModelSpec("claude-opus-4-8", "Claude Opus 4.8", ALL_EFFORTS),
    ModelSpec("claude-opus-4-7", "Claude Opus 4.7", ALL_EFFORTS),
    ModelSpec("claude-opus-4-6", "Claude Opus 4.6", NO_XHIGH),
    ModelSpec("claude-sonnet-5", "Claude Sonnet 5", ALL_EFFORTS, True, ("sonnet",)),
    ModelSpec("claude-sonnet-4-6", "Claude Sonnet 4.6", NO_XHIGH),
    ModelSpec("claude-haiku-4-5", "Claude Haiku 4.5", (), True, ("haiku",)),
)

_BY_NAME: dict[str, ModelSpec] = {}
for _spec in CATALOG:
    _BY_NAME[_spec.id] = _spec
    for _alias in _spec.aliases:
        _BY_NAME[_alias] = _spec


_DATED = re.compile(r"-\d{8}$")


def resolve(name: str) -> ModelSpec | None:
    """Look up an alias or full ID.

    ``[1m]`` context suffixes and dated snapshots (``claude-haiku-4-5-20251001``, the form
    claude itself reports) resolve to the undated entry.
    """
    key = name.strip().removesuffix("[1m]")
    return _BY_NAME.get(key) or _BY_NAME.get(_DATED.sub("", key))


def aliases() -> dict[str, str]:
    return {alias: spec.id for spec in CATALOG for alias in spec.aliases}
