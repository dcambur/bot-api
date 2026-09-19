"""The component contract: what a rendered answer is made of.

The model does not write UI code. It fills one of these typed components (chosen by
``type``), the JSON Schema of which is passed to ``claude --json-schema``. Hosts render
the tree with ``web/bot-answer.js`` (any DOM host) or :func:`render_html` (Python).
"""

from __future__ import annotations

import html
import json
from typing import Annotated, Any, Final, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

COMPONENT_VERSION: Final = "1"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Segment(_Strict):
    surface: str = Field(description="Exact substring of the source sentence.")
    reading: str | None = Field(default=None, description="Hiragana or pinyin; null if none.")
    gloss: str = Field(
        description="Meaning in at most 4 words; for a particle, its function in 1-2 words."
    )
    role: Literal["word", "particle", "aux", "punct"] = "word"
    base: str | None = Field(default=None, description="Dictionary form when conjugated.")


class GrammarNote(_Strict):
    pattern: str = Field(description="Named pattern, e.g. 〜ている or 已经 + Verb + 了.")
    note: str = Field(description="One line.")


class Explanation(_Strict):
    type: Literal["explanation"]
    source: str = Field(description="The sentence as given.")
    translation: str
    segments: list[Segment]
    grammar: list[GrammarNote] = Field(default_factory=list)
    style: str | None = Field(default=None, description="Politeness and tense, e.g. 'plain, past'.")
    nuance: str | None = None


class Example(_Strict):
    text: str
    translation: str


class WordCard(_Strict):
    type: Literal["word"]
    headword: str
    reading: str | None = None
    base: str | None = None
    pos: list[str] = Field(default_factory=list)
    meanings: list[str]
    example: Example | None = None
    note: str | None = None


class ComparisonRow(_Strict):
    aspect: str
    left: str
    right: str


class Comparison(_Strict):
    type: Literal["comparison"]
    title: str
    left: str
    right: str
    rows: list[ComparisonRow]
    summary: str | None = None


class Step(_Strict):
    label: str
    detail: str


class Steps(_Strict):
    type: Literal["steps"]
    title: str | None = None
    steps: list[Step]


class Text(_Strict):
    type: Literal["text"]
    markdown: str = Field(description="Plain paragraphs; **bold**, `code`, '- ' bullets only.")


Component = Annotated[
    Explanation | WordCard | Comparison | Steps | Text, Field(discriminator="type")
]


class Answer(_Strict):
    component_version: Literal["1"] = COMPONENT_VERSION
    component: Component


def _simplify(node: Any) -> Any:
    """Make pydantic's schema palatable to structured outputs: anyOf, no discriminator."""
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key == "discriminator":
                continue
            if key == "oneOf":
                key = "anyOf"
            out[key] = _simplify(value)
        return out
    if isinstance(node, list):
        return [_simplify(item) for item in node]
    return node


def schema() -> dict[str, Any]:
    return cast(dict[str, Any], _simplify(Answer.model_json_schema()))


COMPONENT_RULES = (
    "Respond with one component matching the provided JSON schema. Choose: "
    "'explanation' for a sentence or phrase; 'word' for a single word or short lookup; "
    "'comparison' when two things are contrasted; 'steps' for a procedure; 'text' otherwise. "
    "In explanation.segments cover the whole sentence in order, punctuation included "
    "(role 'punct', empty gloss); readings in hiragana or pinyin with tone marks; particles "
    "get role 'particle' with a one-word function as gloss (subject, topic, object, of, with, "
    "question); "
    "every other gloss is at most 4 words."
)


# --- Python renderer (previews, tests, non-JS hosts) ---------------------------------------


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _ruby(surface: str, reading: str | None) -> str:
    if reading and reading != surface:
        return f"<ruby>{_e(surface)}<rt>{_e(reading)}</rt></ruby>"
    return _e(surface)


def _render_explanation(c: Explanation) -> str:
    segs = "".join(
        f'<span class="seg seg-{c_.role}"'
        + (f' data-base="{_e(c_.base)}"' if c_.base else "")
        + ">"
        f'<span class="surface">{_ruby(c_.surface, c_.reading)}</span>'
        f'<span class="gloss">{_e(c_.gloss)}</span></span>'
        for c_ in c.segments
    )
    grammar = "".join(
        f'<li><span class="pattern">{_e(g.pattern)}</span> <span>{_e(g.note)}</span></li>'
        for g in c.grammar
    )
    style = f'<span class="style">{_e(c.style)}</span>' if c.style else ""
    parts = [
        f'<div class="head"><p class="translation">{_e(c.translation)}</p>{style}</div>',
        f'<div class="gloss-row">{segs}</div>',
    ]
    if grammar:
        parts.append(f'<ul class="grammar">{grammar}</ul>')
    if c.nuance:
        parts.append(f'<p class="nuance">{_e(c.nuance)}</p>')
    return "".join(parts)


def _render_word(c: WordCard) -> str:
    pos = "".join(f'<span class="tag">{_e(p)}</span>' for p in c.pos)
    meanings = "".join(f"<li>{_e(m)}</li>" for m in c.meanings)
    out = [
        f'<p class="headword">{_ruby(c.headword, c.reading)}'
        + (f' <span class="base">{_e(c.base)}</span>' if c.base else "")
        + f" {pos}</p>",
        f'<ol class="meanings">{meanings}</ol>',
    ]
    if c.example:
        out.append(
            f'<p class="example"><span class="ja">{_e(c.example.text)}</span>'
            f'<span class="en">{_e(c.example.translation)}</span></p>'
        )
    if c.note:
        out.append(f'<p class="nuance">{_e(c.note)}</p>')
    return "".join(out)


def _render_comparison(c: Comparison) -> str:
    rows = "".join(
        f"<tr><th>{_e(r.aspect)}</th><td>{_e(r.left)}</td><td>{_e(r.right)}</td></tr>"
        for r in c.rows
    )
    out = [
        f'<p class="title">{_e(c.title)}</p>',
        f'<table class="compare"><thead><tr><th></th><th>{_e(c.left)}</th>'
        f"<th>{_e(c.right)}</th></tr></thead><tbody>{rows}</tbody></table>",
    ]
    if c.summary:
        out.append(f'<p class="nuance">{_e(c.summary)}</p>')
    return "".join(out)


def _render_steps(c: Steps) -> str:
    items = "".join(
        f'<li><span class="label">{_e(s.label)}</span> <span>{_e(s.detail)}</span></li>'
        for s in c.steps
    )
    title = f'<p class="title">{_e(c.title)}</p>' if c.title else ""
    return f'{title}<ol class="steps">{items}</ol>'


def _inline(text: str) -> str:
    """Escape, then re-enable the two inline marks the contract allows."""
    out = _e(text)
    while "**" in out:
        first = out.find("**")
        second = out.find("**", first + 2)
        if second < 0:
            break
        out = out[:first] + "<b>" + out[first + 2 : second] + "</b>" + out[second + 2 :]
    while "`" in out:
        first = out.find("`")
        second = out.find("`", first + 1)
        if second < 0:
            break
        out = out[:first] + "<code>" + out[first + 1 : second] + "</code>" + out[second + 1 :]
    return out


def _render_text(c: Text) -> str:
    blocks: list[str] = []
    bullets: list[str] = []

    def flush() -> None:
        if bullets:
            blocks.append("<ul>" + "".join(f"<li>{b}</li>" for b in bullets) + "</ul>")
            bullets.clear()

    for para in c.markdown.strip().split("\n\n"):
        for line in para.splitlines():
            if line.startswith("- "):
                bullets.append(_inline(line[2:]))
            else:
                flush()
                blocks.append(f"<p>{_inline(line)}</p>")
        flush()
    return "".join(blocks)


def render_html(answer: Answer) -> str:
    """Minimal semantic HTML for the component; class names match bot-answer.js."""
    c = answer.component
    if isinstance(c, Explanation):
        body = _render_explanation(c)
    elif isinstance(c, WordCard):
        body = _render_word(c)
    elif isinstance(c, Comparison):
        body = _render_comparison(c)
    elif isinstance(c, Steps):
        body = _render_steps(c)
    else:
        body = _render_text(c)
    return f'<div class="bot-answer bot-answer-{c.type}">{body}</div>'


def to_json(answer: Answer) -> str:
    return json.dumps(answer.model_dump(mode="json", exclude_none=True), ensure_ascii=False)
