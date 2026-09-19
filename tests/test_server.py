from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

import pytest

from bot_api.server import make_server
from tests.conftest import FakeClaude


@pytest.fixture
def base_url(fake_claude: FakeClaude) -> Iterator[str]:
    server = make_server("127.0.0.1", 0, cors="http://example.test")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _call(url: str, method: str = "GET", body: Any = None) -> tuple[int, dict[str, str], Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            raw = res.read()
            return (
                res.status,
                dict(res.headers),
                (json.loads(raw) if raw.startswith(b"{") or raw.startswith(b"[") else raw),
            )
    except urllib.error.HTTPError as err:
        raw = err.read()
        return err.code, dict(err.headers), json.loads(raw)


def test_health_skills_schema_and_static(base_url: str) -> None:
    status, headers, body = _call(f"{base_url}/health")
    assert status == 200 and body == {"ok": True}
    assert headers["Access-Control-Allow-Origin"] == "http://example.test"
    status, _, skills = _call(f"{base_url}/skills")
    assert status == 200 and {s["name"] for s in skills} >= {"ja", "zh"}
    status, _, schema = _call(f"{base_url}/schema")
    assert status == 200 and "component" in schema and "request" in schema
    status, headers, js = _call(f"{base_url}/static/bot-answer.js")
    assert status == 200 and b"customElements.define('bot-answer'" in js
    assert headers["Content-Type"].startswith("text/javascript")
    status, _, page = _call(f"{base_url}/")
    assert status == 200 and b"<bot-answer" in page
    status, _, _ = _call(f"{base_url}/static/../pyproject.toml")
    assert status == 404


def test_ask_roundtrip_and_errors(base_url: str, fake_claude: FakeClaude) -> None:
    status, _, body = _call(f"{base_url}/ask", "POST", {"prompt": "hi", "skill": "ja"})
    assert (
        status == 200
        and body["ok"] is True
        and body["text"] == "echo: hi"
        and body["skill"] == "ja"
    )
    status, _, body = _call(f"{base_url}/ask", "POST", {"prompt": ""})
    assert status == 400 and body["error"]["code"] == "invalid_request"
    fake_claude.respond(is_error=True, api_error_status=404, result="issue with the selected model")
    status, _, body = _call(f"{base_url}/ask", "POST", {"prompt": "hi"})
    assert status == 400 and body["error"]["code"] == "invalid_model"
    status, _, body = _call(f"{base_url}/nope", "POST", {"prompt": "hi"})
    assert status == 404


def test_options_preflight(base_url: str) -> None:
    req = urllib.request.Request(f"{base_url}/ask", method="OPTIONS")
    with urllib.request.urlopen(req, timeout=10) as res:
        assert res.status == 204
        assert res.headers["Access-Control-Allow-Methods"] == "GET, POST, OPTIONS"
