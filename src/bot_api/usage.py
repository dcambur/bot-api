"""Per-call ledger: one JSON object per line in ``<config dir>/usage.jsonl``.

Every ``ask``/``stream_ask`` appends a line (successes and failures alike) unless
``usage_log = false`` in the settings. ``bot usage`` summarizes it; on a subscription this
is the only way to see how much of the rolling window a host like yomi-overlay's ⌘E is
using.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import config

_SINCE = re.compile(r"^(\d+(?:\.\d+)?)\s*([mhd])$")
_UNITS = {"m": "minutes", "h": "hours", "d": "days"}


def path() -> Path:
    return config.config_path().parent / "usage.jsonl"


def append(entry: dict[str, Any]) -> None:
    """Best effort: a ledger that cannot be written must never fail the answer."""
    try:
        target = path()
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def parse_since(text: str) -> datetime | None:
    """``5h``, ``30m``, ``7d`` → cutoff; ``all`` → None. Raises ValueError."""
    if text.strip().lower() == "all":
        return None
    match = _SINCE.match(text.strip().lower())
    if not match:
        raise ValueError(f"invalid duration {text!r}; use e.g. 30m, 5h, 7d or all")
    amount, unit = match.groups()
    return datetime.now(UTC) - timedelta(**{_UNITS[unit]: float(amount)})


def read(since: datetime | None = None) -> list[dict[str, Any]]:
    target = path()
    if not target.exists():
        return []
    out: list[dict[str, Any]] = []
    with target.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            if since is not None:
                try:
                    when = datetime.fromisoformat(str(entry.get("ts")))
                except ValueError:
                    continue
                if when < since:
                    continue
            out.append(entry)
    return out


def summarize(entries: list[dict[str, Any]]) -> dict[str, Any]:
    totals: dict[str, float] = defaultdict(float)
    by_model: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    by_skill: dict[str, int] = defaultdict(int)
    for e in entries:
        ok = bool(e.get("ok"))
        totals["calls"] += 1
        totals["ok" if ok else "errors"] += 1
        by_skill[str(e.get("skill") or "(none)")] += 1
        model = str(e.get("model") or "?")
        by_model[model]["calls"] += 1
        if not ok:
            continue
        for key in (
            "input_tokens",
            "output_tokens",
            "thinking_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "cost_usd",
            "duration_ms",
        ):
            value = float(e.get(key) or 0)
            totals[key] += value
            by_model[model][key] += value
    return {
        "calls": int(totals["calls"]),
        "ok": int(totals["ok"]),
        "errors": int(totals["errors"]),
        "input_tokens": int(totals["input_tokens"]),
        "output_tokens": int(totals["output_tokens"]),
        "thinking_tokens": int(totals["thinking_tokens"]),
        "cache_read_input_tokens": int(totals["cache_read_input_tokens"]),
        "cache_creation_input_tokens": int(totals["cache_creation_input_tokens"]),
        "cost_usd": round(totals["cost_usd"], 4),
        "duration_ms": int(totals["duration_ms"]),
        "by_model": {
            m: {
                "calls": int(v["calls"]),
                "output_tokens": int(v["output_tokens"]),
                "cost_usd": round(v["cost_usd"], 4),
            }
            for m, v in sorted(by_model.items(), key=lambda kv: -kv[1]["calls"])
        },
        "by_skill": dict(sorted(by_skill.items(), key=lambda kv: -kv[1])),
    }
