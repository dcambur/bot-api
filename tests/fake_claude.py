#!/usr/bin/env python3
"""Stand-in for the `claude` binary used by the test-suite.

Records argv/stdin/env to $FAKE_CLAUDE_LOG and replies with $FAKE_CLAUDE_RESPONSE
(a JSON result payload) or a default success. Honors --output-format stream-json.
"""

import json
import os
import sys
import time

args = sys.argv[1:]
stdin = sys.stdin.read() if not sys.stdin.isatty() else ""

if log := os.environ.get("FAKE_CLAUDE_LOG"):
    with open(log, "w") as fh:
        json.dump(
            {
                "argv": args,
                "stdin": stdin,
                "cwd": os.getcwd(),
                "env": {
                    k: os.environ.get(k)
                    for k in (
                        "MAX_THINKING_TOKENS",
                        "CLAUDE_CODE_EFFORT_LEVEL",
                        "ANTHROPIC_MODEL",
                        "ANTHROPIC_API_KEY",
                        "CLAUDE_CODE_USE_BEDROCK",
                    )
                },
            },
            fh,
        )

if args[:1] == ["--version"]:
    print("9.9.9 (Claude Code)")
    sys.exit(0)
if args[:1] == ["--help"]:
    print("Usage: claude [options] [command] [prompt]")
    if not os.environ.get("FAKE_CLAUDE_OLD"):
        print("  --safe-mode   Start with all customizations disabled")
    sys.exit(0)
if args[:2] == ["auth", "status"]:
    print(json.dumps({"loggedIn": True, "email": "t@example.com", "subscriptionType": "max"}))
    sys.exit(0)

if delay := os.environ.get("FAKE_CLAUDE_SLEEP"):
    time.sleep(float(delay))


def arg_after(flag: str) -> str | None:
    return args[args.index(flag) + 1] if flag in args else None


model = arg_after("--model") or "claude-sonnet-5"
payload = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": f"echo: {stdin.strip()}",
    "session_id": "sess-123",
    "duration_ms": 42,
    "stop_reason": "end_turn",
    "total_cost_usd": 0.001,
    "usage": {
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_input_tokens": 1,
        "cache_creation_input_tokens": 2,
        "output_tokens_details": {"thinking_tokens": 3},
    },
    "modelUsage": {model: {"inputTokens": 10}},
}
if resp := os.environ.get("FAKE_CLAUDE_RESPONSE"):
    payload.update(json.loads(resp))

if arg_after("--output-format") == "stream-json":
    print(json.dumps({"type": "system", "subtype": "init", "session_id": "sess-123"}))
    text = payload.get("result") or ""
    half = len(text) // 2
    for chunk in (text[:half], text[half:]):
        print(
            json.dumps(
                {
                    "type": "stream_event",
                    "event": {
                        "type": "content_block_delta",
                        "delta": {"type": "text_delta", "text": chunk},
                    },
                }
            )
        )
    print(json.dumps(payload))
else:
    if os.environ.get("FAKE_CLAUDE_NOISE"):
        print('[claude-code:some_log] {"noise":true}')
    print(json.dumps(payload))
sys.exit(1 if payload.get("is_error") else 0)
