from bot_api import Effort
from bot_api.catalog import CATALOG, aliases, resolve


def test_aliases_resolve_to_catalog_entries() -> None:
    assert resolve("sonnet") is resolve("claude-sonnet-5")
    assert resolve("opus").id == "claude-opus-5"  # type: ignore[union-attr]
    assert resolve("fable").id == "claude-fable-5-1"  # type: ignore[union-attr]
    assert resolve("best") is resolve("fable")
    assert set(aliases()) == {"fable", "best", "opus", "sonnet", "haiku"}


def test_context_suffix_and_unknown() -> None:
    assert resolve("sonnet[1m]") is resolve("sonnet")
    assert resolve("claude-nope") is None


def test_effort_support_matrix() -> None:
    assert resolve("claude-opus-4-6").supports_effort(Effort.xhigh) is False  # type: ignore[union-attr]
    assert resolve("claude-opus-5").supports_effort(Effort.xhigh) is True  # type: ignore[union-attr]
    assert resolve("haiku").efforts == ()  # type: ignore[union-attr]
    assert all(not s.thinking_switchable for s in CATALOG if s.id.startswith("claude-fable"))
