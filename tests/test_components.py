from __future__ import annotations

import json

import pytest

from bot_api import AskRequest, ask
from bot_api.components import (
    COMPONENT_RULES,
    Answer,
    Explanation,
    Segment,
    Text,
    render_html,
    schema,
)
from bot_api.config import Settings
from tests.conftest import FakeClaude

TREE = {
    "component": {
        "type": "explanation",
        "source": "猫が見ている。",
        "translation": "The cat is looking.",
        "segments": [
            {"surface": "猫", "reading": "ねこ", "gloss": "cat"},
            {"surface": "が", "gloss": "subject", "role": "particle"},
            {"surface": "見ている", "reading": "みている", "base": "見る", "gloss": "is looking"},
            {"surface": "。", "gloss": "", "role": "punct"},
        ],
        "grammar": [{"pattern": "〜ている", "note": "ongoing"}],
        "style": "plain",
    }
}


def test_schema_is_structured_output_friendly() -> None:
    s = schema()
    text = json.dumps(s)
    assert "oneOf" not in text and "discriminator" not in text
    assert set(s["properties"]) == {"component_version", "component"}
    assert len(s["properties"]["component"]["anyOf"]) == 5


def test_answer_validates_and_rejects_unknown_type() -> None:
    answer = Answer.model_validate(TREE)
    assert isinstance(answer.component, Explanation)
    with pytest.raises(ValueError):
        Answer.model_validate({"component": {"type": "chart", "data": []}})
    with pytest.raises(ValueError):
        Answer.model_validate({"component": {"type": "text", "markdown": "x", "extra": 1}})


def test_render_html_explanation_escapes_and_marks_roles() -> None:
    tree = Answer.model_validate(TREE)
    html = render_html(tree)
    assert html.startswith('<div class="bot-answer bot-answer-explanation">')
    assert "<ruby>猫<rt>ねこ</rt></ruby>" in html
    assert 'class="seg seg-particle"' in html and 'data-base="見る"' in html
    assert '<span class="pattern">〜ている</span>' in html
    evil = Answer(component=Text(type="text", markdown="<script>x</script> **b** `c`\n\n- one"))
    out = render_html(evil)
    assert "<script>" not in out and "&lt;script&gt;" in out
    assert "<b>b</b>" in out and "<code>c</code>" in out and "<ul><li>one</li></ul>" in out


def test_segment_reading_omitted_when_same() -> None:
    seg = Segment(surface="が", gloss="subject", role="particle")
    html = render_html(
        Answer(
            component=Explanation(type="explanation", source="が", translation="", segments=[seg])
        )
    )
    assert "<ruby>" not in html


def test_component_mode_sets_schema_and_rules(fake_claude: FakeClaude) -> None:
    fake_claude.respond(structured_output=TREE)
    resp = ask(AskRequest(prompt="猫が見ている。", skill="ja", component=True), Settings())
    argv = fake_claude.argv()
    sent = json.loads(argv[argv.index("--json-schema") + 1])
    assert sent == schema()
    assert argv[argv.index("--system-prompt") + 1].endswith(COMPONENT_RULES)
    assert resp.structured == TREE and resp.warnings == []


def test_component_mode_warns_on_bad_tree(fake_claude: FakeClaude) -> None:
    fake_claude.respond(structured_output={"component": {"type": "nope"}})
    resp = ask(AskRequest(prompt="q", component=True), Settings())
    assert any("failed validation" in w for w in resp.warnings)
    fake_claude.respond(structured_output=None)
    resp = ask(AskRequest(prompt="q", component=True), Settings())
    assert any("no structured output" in w for w in resp.warnings)


def test_explicit_schema_beats_component_schema(fake_claude: FakeClaude) -> None:
    ask(AskRequest(prompt="q", component=True, output_schema={"type": "object"}), Settings())
    argv = fake_claude.argv()
    assert json.loads(argv[argv.index("--json-schema") + 1]) == {"type": "object"}
