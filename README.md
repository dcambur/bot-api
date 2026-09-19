# bot-api

Chat with Claude from the terminal — or from any other program — through the
Claude Code CLI (`claude -p`), so it runs on your Claude subscription rather than
an API key.

```
$ bot ask "猫が窓の外を見ている を説明して"
The sentence means "The cat is looking outside the window." ...
```

## Install

Requires Python ≥ 3.13, [uv](https://docs.astral.sh/uv/) and the Claude Code CLI:

```bash
npm install -g @anthropic-ai/claude-code   # once
claude auth login                          # once, opens your browser
uv tool install --editable .               # puts `bot` on PATH at ~/.local/bin/bot
bot doctor                                 # verifies everything is wired up
```

Other programs call `~/.local/bin/bot`, which is where `uv tool install` puts it
(yomi-overlay looks there by default). To hack on bot-api itself without installing,
`uv sync` and prefix commands with `uv run`.

`bot` finds `claude` on `PATH`, in `~/.npm-global/bin`, `~/.claude/local`, `~/.local/bin`,
Homebrew, or wherever `BOT_API_CLAUDE_BIN` / the `claude_bin` setting points.

## Usage

```bash
bot ask "What does the particle は do?"          # plain answer
bot ask "Explain:" < sentence.txt                 # piped stdin is appended to the prompt
bot ask "..." --stream                            # tokens as they arrive
bot ask "..." -m opus --effort high --no-thinking # per-call overrides
bot ask "..." --json                              # AskResult JSON (see contract below)
bot ask "..." --schema words.schema.json          # structured output
bot ask "..." --persist --json                    # keep the session → session_id
bot ask "Continue" --session <id>                 # follow-up in that session

bot ask -k ja "猫が窓の外を見ている"              # skill: Japanese sentence explanation
bot ask -k none "what is 2+2"                     # skip the configured default skill for one call
bot chat                                           # multi-turn REPL; /new, /session, /exit
bot ask -k zh -c "我已经吃过饭了。"                # component mode: typed UI tree as JSON
bot ask -k ja -c --render html "..."              # same, rendered to HTML by bot-api

bot skills list|show|use|export|path              # ja, zh bundled; your own in ~/.config/bot-api/skills/
bot serve --allow-origin chrome-extension://<id>  # http://127.0.0.1:7788 — POST /ask + playground
bot usage [--since 5h|24h|7d|all] [--tail 5]       # what the last window cost (usage.jsonl ledger)
bot models list            # catalog: aliases, effort levels, thinking toggle
bot models current         # the configured default
bot models set opus        # or a full ID; --allow-unknown for IDs not in the catalog
bot thinking on|off        # extended thinking (cannot be turned off on Fable models)
bot thinking effort xhigh  # low|medium|high|xhigh|max|default (validated per model)
bot config show|set|path   # config.toml lives in ~/.config/bot-api/ (or $BOT_API_CONFIG)
bot config schema          # JSON Schema of the request/response contract
```

## Skills

A skill is a Markdown file with TOML front matter: a compact system prompt (~250
tokens, written to work with thinking off on Sonnet) plus optional defaults for
`model`, `thinking_enabled`, `effort`. Bundled: `ja` (Japanese) and `zh` (Mandarin),
both shaped as *translation → word-by-word breakdown with readings → grammar patterns →
one-line nuance*. `bot skills export ja` copies one into `~/.config/bot-api/skills/`
where your edits shadow the bundled version; any new `<name>.md` there is a new skill.

Precedence for every setting: request flags > skill > `config.toml`. `-k none` (or
`"skill": "none"` in a request) drops the configured default skill for that call.

## Components (typed UI output)

`--component` (or `"component": true` in a request) asks the model to fill one of five
typed components instead of prose — it picks the `type`:

| type | when | shape |
|---|---|---|
| `explanation` | a sentence or phrase | source, translation, `segments[]` (surface, reading, gloss, role, base), `grammar[]` (pattern, note), style, nuance |
| `word` | a single word | headword, reading, pos, meanings, example, note |
| `comparison` | X vs Y | title, left, right, rows[] (aspect, left, right), summary |
| `steps` | a procedure | title, steps[] (label, detail) |
| `text` | anything else | markdown (paragraphs, **bold**, `code`, bullets) |

The tree comes back in `AskResponse.structured`, validated against
`bot config schema --component`. Rendering is deterministic and lives in bot-api, not
in the model:

- **`src/bot_api/web/bot-answer.js`** — a zero-dependency `<bot-answer>` web component
  (classic script or ES module). `el.result = askResult` renders it; `el.loading = true`
  shows a skeleton; clicking a word fires a `lookup` event with `{surface, reading, base}`
  so the host opens its own dictionary. Themed only through CSS variables
  (`--ba-fg`, `--ba-accent`, `--ba-muted`, `--ba-rule`, `--ba-font`, `--ba-font-cjk`, `--ba-size`),
  so it looks native inside yomi-overlay's dark gold popup and nantan's slate/green UI alike.
- **`bot ask -c --render html`** / `bot_api.components.render_html()` — the same tree as
  escaped HTML with matching class names, for previews and non-JS hosts.

Integration notes: [docs/integration-yomi-overlay.md](docs/integration-yomi-overlay.md)
(⌘E → explain the sentence under the cursor) and
[docs/integration-nantan.md](docs/integration-nantan.md) (extension + React).

## Chat

`bot chat` is a REPL over the same machinery: every turn is one `bot ask --persist` that
resumes the previous turn's session, streamed as it arrives. `/new` starts a fresh session,
`/session` prints the id, `/exit` or Ctrl-D quits and prints a `bot chat --session <id>` hint
so the conversation can be picked up later. It takes the same `-m`, `-k`, `--effort`,
`--thinking`, `-s` flags as `ask`.

## Usage ledger

Every call — CLI, HTTP, Python, success or failure — appends one JSON line to
`~/.config/bot-api/usage.jsonl` (`usage_log = false` turns it off). `bot usage` sums the
last 5 hours by default (a subscription window); `--since 24h`, `--tail 10`, `--json`.
The `cost_usd` column is what the API would have charged; on a subscription it is a
proxy for how much of the window a host like ⌘E is spending.

## HTTP mode

`bot serve` (default `127.0.0.1:7788`) exposes the contract to anything that can
`fetch()` — browser extensions, Electron renderers:

```
POST /ask        AskRequest → AskResult (HTTP 200, or 4xx/5xx mapped from error.code)
GET  /health     {"ok": true}
GET  /skills     the skill list
GET  /schema     request + component JSON Schemas
GET  /           playground: try skills/models/themes, see the component render live
GET  /static/bot-answer.js
```

Browser origins are an allowlist. A request carrying an `Origin` header is accepted only
from the playground itself, from an origin given with `--allow-origin` (repeatable, e.g.
`chrome-extension://<id>`), or from anywhere with `--allow-origin '*'`; anything else is
`403`. Without this any web page you visit could spend your subscription through the
loopback address. Requests without `Origin` (curl, Electron main) are always accepted.

Each request runs one `claude -p`, so at most `--max-concurrent` (default 2) run at once;
a request that gets no slot within `--queue-timeout` seconds (default 15) is answered
`503 busy` instead of piling up processes.

## Message contract (v1)

Everything — CLI, Python API, and other services — speaks the same two shapes.

**Request** (`bot ask --request` reads one from stdin):

```json
{
  "prompt": "猫が窓の外を見ている",
  "system_prompt": "You are a Japanese tutor.",
  "model": "sonnet",
  "skill": "ja",
  "component": false,
  "thinking": {"enabled": true, "effort": "low"},
  "session_id": null,
  "persist_session": false,
  "output_schema": {"type": "object", "properties": {"translation": {"type": "string"}}},
  "timeout_s": 120
}
```

Only `prompt` is required; every other field falls back to the configured defaults.

**Response** — always `ok: true` with the answer, or `ok: false` with a stable error code:

```json
{"ok": true, "text": "...", "structured": {"translation": "..."}, "model": "claude-sonnet-5",
 "session_id": "…", "usage": {"input_tokens": 742, "output_tokens": 40, "thinking_tokens": 0, "...": 0},
 "cost_usd": 0.0015, "duration_ms": 2885, "stop_reason": "end_turn", "skill": "ja", "warnings": []}

{"ok": false, "error": {"code": "invalid_model", "message": "...", "details": {"api_error_status": 404}}}
```

Error codes: `invalid_request`, `claude_not_found`, `not_authenticated`, `invalid_model`,
`unsupported_effort`, `timeout`, `claude_error`, `bad_output`, `busy` (HTTP only). Exit
status is 0 on success, 1 on any error, 2 on usage mistakes.

`session_id` is set only when the request asked for persistence (`persist_session` or
`session_id`); a one-shot answer cannot be resumed, so it reports `null`. In component
mode a valid tree is the answer and `text` is `""`; if the tree fails validation the raw
text stays and a warning says why.

### From Python (e.g. an OCR pipeline)

```python
from bot_api import AskRequest, ask_result

result = ask_result(AskRequest(prompt=ocr_text, skill="ja", component=True))
if result.ok:
    tree = result.structured          # feed to <bot-answer>, or:
    from bot_api.components import Answer, render_html
    html = render_html(Answer.model_validate(tree))
else:
    log.error("%s: %s", result.error.code, result.error.message)
```

`ask()` raises `BotApiError` instead of returning `AskError`; `stream_ask(req, on_text)`
delivers chunks as they arrive.

### From any other language

```bash
echo '{"prompt":"..."}' | bot ask --request
```

## How it works

`bot` spawns `claude -p` with the prompt on stdin and these flags:
`--output-format json --tools "" --max-turns 1 --setting-sources "" [--safe-mode]
--system-prompt … --model … [--effort …] [--json-schema …]
[--no-session-persistence | --resume <id>]`.

- No tools, one turn, no project/user settings, no hooks or MCP servers: it behaves like
  a chat model, not a coding agent.
- `--bare` is deliberately **not** used because it disables subscription login.
  `--safe-mode` (Claude Code ≥ 2.1) is: it skips plugins, hooks, MCP and CLAUDE.md while
  auth works normally, and cuts ~1.5 s of start-up per call. `bot` checks once per
  `claude` binary whether the flag exists (cached in `cli-cache.json`); `safe_mode = false`
  turns it off.
- `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` and the Bedrock / Vertex / Foundry switches
  are stripped from the child's environment so a stray key cannot silently move you off
  the subscription; `keep_auth_env = true` passes them through. `bot doctor` lists any
  that are set.
- Thinking off ⇒ `MAX_THINKING_TOKENS=0` in the child environment (no effect on Fable).
  `CLAUDE_CODE_EFFORT_LEVEL` is stripped because it would override `--effort`.
- The child runs in `~/.config/bot-api/workdir` so Claude Code's per-project state never
  lands in your current directory.
- Every call pays Claude Code's own start-up before the model is even asked: about 3 s
  measured on Claude Code 2.1.x with `--safe-mode`, 4.5 s without (Python's share is
  ~0.3 s). Budget 3 s of fixed cost plus 1–6 s of model time.
- Streaming has the same deadline as a plain ask: a watchdog kills a child that stalls
  mid-answer, so `timeout_s` is honoured whether or not tokens were already flowing.
- The CLI has no "list models" command, so `bot models list` is a hand-maintained catalog
  (`src/bot_api/catalog.py`); unknown IDs are passed through unvalidated.

## Development

```bash
uv run pytest        # unit tests run against tests/fake_claude.py, no network
uv run ruff check src tests && uv run ruff format --check src tests
uv run mypy
```
