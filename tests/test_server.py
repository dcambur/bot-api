from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

import pytest

from bot_api.server import make_server
from tests.conftest import FakeClaude

EXT = "chrome-extension://abcdefghijklmnop"


@pytest.fixture
def base_url(fake_claude: FakeClaude) -> Iterator[str]:
    server = make_server("127.0.0.1", 0, allow_origins=[EXT], max_concurrent=1, queue_timeout_s=0.2)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _call(
    url: str,
    method: str = "GET",
    body: Any = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
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
    assert "Access-Control-Allow-Origin" not in headers  # no Origin: not a browser
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


def test_browser_origins_are_an_allowlist(base_url: str) -> None:
    same = base_url  # the playground
    for origin, expect in ((EXT, 200), (same, 200), ("https://evil.example", 403)):
        status, headers, body = _call(
            f"{base_url}/ask", "POST", {"prompt": "hi"}, headers={"Origin": origin}
        )
        assert status == expect, origin
        if expect == 200:
            assert headers["Access-Control-Allow-Origin"] == origin and headers["Vary"] == "Origin"
        else:
            assert "Access-Control-Allow-Origin" not in headers
            assert (
                body["error"]["code"] == "invalid_request"
                and "allow-origin" in body["error"]["message"]
            )
    status, _, _ = _call(f"{base_url}/skills", headers={"Origin": "https://evil.example"})
    assert status == 403  # reads leak prompts and paths too
    # an explicit "*" opens it up
    server = make_server("127.0.0.1", 0, allow_origins=["*"])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        status, headers, _ = _call(f"{url}/health", headers={"Origin": "https://evil.example"})
        assert status == 200 and headers["Access-Control-Allow-Origin"] == "*"
    finally:
        server.shutdown()
        server.server_close()


def test_options_preflight(base_url: str) -> None:
    for origin, expect in ((EXT, 204), ("https://evil.example", 403)):
        req = urllib.request.Request(
            f"{base_url}/ask", method="OPTIONS", headers={"Origin": origin}
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as res:
                status, headers = res.status, dict(res.headers)
        except urllib.error.HTTPError as err:
            status, headers = err.code, dict(err.headers)
        assert status == expect, origin
        if expect == 204:
            assert headers["Access-Control-Allow-Methods"] == "GET, POST, OPTIONS"
            assert headers["Access-Control-Allow-Origin"] == origin


def test_bad_content_length_and_oversized_body(base_url: str) -> None:
    req = urllib.request.Request(
        f"{base_url}/ask", data=b"{}", method="POST", headers={"Content-Length": "abc"}
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        raise AssertionError("expected 400")
    except urllib.error.HTTPError as err:
        assert err.code == 400 and json.loads(err.read())["error"]["code"] == "invalid_request"
    # The server answers before reading an oversized body, so speak raw HTTP: headers only.
    host, port = base_url.removeprefix("http://").split(":")
    with socket.create_connection((host, int(port)), timeout=10) as sock:
        sock.sendall(
            f"POST /ask HTTP/1.1\r\nHost: {host}:{port}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {2 << 20}\r\n\r\n".encode()
        )
        raw = sock.recv(65536).decode()
    assert raw.startswith("HTTP/1.0 400") and "exceeds" in raw


def test_concurrency_cap_answers_busy(base_url: str, fake_claude: FakeClaude) -> None:
    fake_claude.monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "1.5")
    results: list[tuple[int, Any]] = []

    def call() -> None:
        status, _, body = _call(f"{base_url}/ask", "POST", {"prompt": "slow"})
        results.append((status, body))

    threads = [threading.Thread(target=call) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    statuses = sorted(s for s, _ in results)
    assert statuses == [200, 503], results
    busy = next(b for s, b in results if s == 503)
    assert (
        busy["error"]["code"] == "busy" and "1 requests already running" in busy["error"]["message"]
    )
