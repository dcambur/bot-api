"""Local HTTP surface: the same contract over ``POST /ask`` for hosts that can only fetch().

Stdlib only. One thread per request; each request spawns one ``claude -p``.
"""

from __future__ import annotations

import json
import mimetypes
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError

from . import components, skillset
from .contract import AskError, AskRequest, ErrorCode, ErrorInfo
from .runner import ask_result

STATUS_BY_CODE: dict[ErrorCode, HTTPStatus] = {
    ErrorCode.invalid_request: HTTPStatus.BAD_REQUEST,
    ErrorCode.invalid_model: HTTPStatus.BAD_REQUEST,
    ErrorCode.unsupported_effort: HTTPStatus.BAD_REQUEST,
    ErrorCode.claude_not_found: HTTPStatus.SERVICE_UNAVAILABLE,
    ErrorCode.not_authenticated: HTTPStatus.SERVICE_UNAVAILABLE,
    ErrorCode.timeout: HTTPStatus.GATEWAY_TIMEOUT,
    ErrorCode.claude_error: HTTPStatus.BAD_GATEWAY,
    ErrorCode.bad_output: HTTPStatus.BAD_GATEWAY,
}


def _web_file(name: str) -> tuple[bytes, str] | None:
    if "/" in name or name.startswith("."):
        return None
    entry = resources.files("bot_api.web") / name
    if not entry.is_file():
        return None
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return entry.read_bytes(), f"{mime}; charset=utf-8" if mime.startswith(
        "text/"
    ) or mime.endswith("javascript") or mime.endswith("json") else mime


class Handler(BaseHTTPRequestHandler):
    cors = "*"
    server_version = "bot-api/0.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        pass

    # -- helpers ---------------------------------------------------------------------

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", self.cors)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: HTTPStatus, payload: Any) -> None:
        self._send(
            status,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _error(self, status: HTTPStatus, code: ErrorCode, message: str) -> None:
        self._json(
            status, AskError(error=ErrorInfo(code=code, message=message)).model_dump(mode="json")
        )

    # -- routes ----------------------------------------------------------------------

    def do_OPTIONS(self) -> None:  # noqa: N802 - stdlib naming
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", self.cors)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/":
            path = "/index.html"
        if path == "/health":
            self._json(HTTPStatus.OK, {"ok": True})
        elif path == "/skills":
            self._json(
                HTTPStatus.OK, [s.model_dump(mode="json") for s in skillset.load_all().values()]
            )
        elif path == "/schema":
            self._json(
                HTTPStatus.OK,
                {"request": AskRequest.model_json_schema(), "component": components.schema()},
            )
        elif path.startswith("/static/") or path == "/index.html":
            found = _web_file(path.removeprefix("/static/").removeprefix("/"))
            if found is None:
                self._error(HTTPStatus.NOT_FOUND, ErrorCode.invalid_request, "not found")
            else:
                self._send(HTTPStatus.OK, *found)
        else:
            self._error(HTTPStatus.NOT_FOUND, ErrorCode.invalid_request, "not found")

    def do_POST(self) -> None:  # noqa: N802
        if urlsplit(self.path).path != "/ask":
            self._error(HTTPStatus.NOT_FOUND, ErrorCode.invalid_request, "not found")
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            request = AskRequest.model_validate_json(raw)
        except ValidationError as exc:
            self._error(
                HTTPStatus.BAD_REQUEST, ErrorCode.invalid_request, str(exc.errors()[0]["msg"])
            )
            return
        result = ask_result(request)
        status = (
            HTTPStatus.OK
            if result.ok
            else STATUS_BY_CODE.get(result.error.code, HTTPStatus.BAD_GATEWAY)
        )
        self._json(status, result.model_dump(mode="json"))


def make_server(host: str = "127.0.0.1", port: int = 7788, cors: str = "*") -> ThreadingHTTPServer:
    handler = type("BotApiHandler", (Handler,), {"cors": cors})
    return ThreadingHTTPServer((host, port), handler)


def run(host: str = "127.0.0.1", port: int = 7788, cors: str = "*") -> None:
    server = make_server(host, port, cors)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
