# Integrating bot-api into nantan

Two hosts, one renderer.

## Browser extension (classic scripts, shadow-DOM popups)

- Add `bot-answer.js` to the content scripts list in `manifest.json` (it defines the
  element once; safe to load in every frame).
- Inside `content/popup-view.js` an "Explain sentence" button creates a `<bot-answer>`
  in the same shadow root as the popup; the popup's own CSS variables carry the theme:

  ```css
  bot-answer { --ba-accent: rgb(var(--c-accent-green)); --ba-muted: rgb(var(--c-text-secondary));
               --ba-rule: rgb(var(--c-surface-border)); --ba-font: Inter, system-ui, sans-serif;
               --ba-font-cjk: "Hiragino Sans", "Noto Sans CJK JP", sans-serif; }
  ```
- Content scripts cannot spawn processes, so they call `POST http://127.0.0.1:7788/ask`
  (`bot serve` must be running; add `http://127.0.0.1:7788/*` to `host_permissions`).
  The `lookup` event re-enters the existing scan → lookup pipeline with `detail.base`.

## React app (frontend/)

Wrap the element once; everything else is data:

```tsx
import { useEffect, useRef } from 'react';
import '../../vendor/bot-answer.js';      // copied from bot-api/src/bot_api/web/

export function BotAnswer({ result, loading }: { result?: unknown; loading?: boolean }) {
  const ref = useRef<HTMLElement & { result: unknown; loading: boolean }>(null);
  useEffect(() => { if (ref.current) ref.current.loading = !!loading; }, [loading]);
  useEffect(() => { if (ref.current && result) ref.current.result = result; }, [result]);
  return <bot-answer ref={ref} className="text-text-primary" />;
}
```

Declare the tag for TSX: `declare global { namespace JSX { interface IntrinsicElements { 'bot-answer': any } } }`.
Theme through the same `--ba-*` variables in `index.css`, mapped to `--c-accent-green`
etc., so the accent picker re-themes explanations for free.

The backend can proxy `/api/explain` → `bot serve` on the host if you would rather not
expose port 7788 to the browser directly.
