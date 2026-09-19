# Integrating bot-api into yomi-overlay

Goal: **⌘E explains the sentence under the cursor** — a separate action from the
Shift-hover dictionary lookup, with its own panel state, never merged into the
dictionary popup.

## Runtime shape

```
Electron main                                renderer
  globalShortcut ⌘E ──► IPC 'explain' ──► sentence around the last hovered glyph
  execFile(bot, ['ask','--request'])  ◄── IPC {prompt, skill:"ja", component:true}
  stdin: AskRequest, stdout: AskResult ──► <bot-answer loading> → .result = json
```

**Transport: a one-shot child, no server.** The renderer's CSP is
`default-src 'none'`, so it cannot `fetch()`; main is the trust boundary and runs
`bot ask --request` per request (JSON on stdin, JSON on stdout):

```js
const { execFile } = require('node:child_process');
function explain(sentence) {
  return new Promise((resolve) => {
    const child = execFile(BOT, ['ask', '--request'], { timeout: 90_000 },
      (err, stdout) => { try { resolve(JSON.parse(stdout)); }
                         catch { resolve({ ok: false, error: { code: 'claude_error', message: String(err) } }); } });
    child.stdin.end(JSON.stringify({ prompt: sentence, skill: 'ja', component: true }));
  });
}
```

Each call costs about 3 s of Claude Code start-up (Python's share is ~0.3 s) plus
2–6 s of model time on Sonnet with thinking off. `bot serve` is for browser
extensions, which cannot spawn processes. `BOT` is `~/.local/bin/bot`, where
`uv tool install --editable .` in the bot-api checkout puts it. The full design lives
in yomi-overlay's `docs/EXPLAIN.md`.

## Renderer side

1. Copy `src/bot_api/web/bot-answer.js` from this repo into `app/renderer/` and add
   `<script src="bot-answer.js"></script>` to `index.html` (classic script; no build step).
2. Theme it from `overlay.css` so it reads as part of the popup:

   ```css
   bot-answer {
     --ba-accent: #d3b072;                 /* the pitch-mark gold already in the popup */
     --ba-muted: #9d9484;
     --ba-rule: rgba(255,255,255,.1);
     --ba-font-cjk: "Hiragino Mincho ProN", serif;
     --ba-font: -apple-system, system-ui, sans-serif;
   }
   ```
3. Sentence extraction: take the OCR line (or column, in `verticalNative`) that
   contains the last hovered glyph and cut at `。！？` on both sides. The glyph layer
   already has per-glyph boxes in reading order, so this is a slice, not new geometry.
4. Panel: reuse the popup shell (same NSPanel, same placement rules), but a distinct
   mode — `popup.dataset.mode = 'explain'` — that shows only `<bot-answer>`, with the
   source sentence as its header. The `lookup` event from a clicked word feeds the
   existing lookup path, so a word in the explanation opens the dictionary popup as if
   it had been Shift-hovered.

   ```js
   const view = document.createElement('bot-answer');
   view.loading = true;
   panel.replaceChildren(view);
   // preload exposes main's explain(sentence) → Promise<AskResult>; it never rejects
   overlay.explain(sentence).then((res) => { view.result = res; });
   view.addEventListener('lookup', (e) => lookup(e.detail.base || e.detail.surface));
   ```
5. Degrade honestly (CONVENTIONS.md): when `bot` is not installed, times out, or exits
   without a result, main resolves an `ok: false` AskResult (`claude_not_found`,
   `timeout`, `claude_error`) and `<bot-answer>` shows the error line — never a stale
   or partial answer.

## Main side

```js
const { globalShortcut } = require('electron');
app.whenReady().then(() => {
  globalShortcut.register('CommandOrControl+E', () => overlay.webContents.send('explain'));
});
```

The Swift CLI already owns the global Shift/click monitor; ⌘E can stay in Electron
because it does not need pointer position, only the last hover the renderer already has.

## Contract reference

- Request/response: `bot config schema` (or `GET /schema`).
- Component tree: `bot config schema --component`; only `explanation`, `word`,
  `comparison`, `steps`, `text` exist, so the renderer never sees an unknown shape.
