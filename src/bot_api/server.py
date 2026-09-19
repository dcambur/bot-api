"""Local HTTP surface: the same contract over ``POST /ask`` for hosts that can only fetch().

Stdlib only. One thread per request; each request spawns one ``claude -p``, so the number
of concurrent requests is capped (``max_concurrent``) and a request that cannot get a slot
within ``queue_timeout_s`` is answered ``busy`` (503) instead of piling up processes.

Browser origins are an allowlist: a request carrying an ``Origin`` header is accepted only
from the playground itself (same origin), from an origin passed at start-up, or from
anywhere when ``*`` was passed. Without this any web page could spend the subscription
through the loopback address. Requests without ``Origin`` (curl, Electron main) are always
accepted.
"""

from __future__ import annotations

import json
import mimetypes
import threading
from collections.abc import Iterable
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
    ErrorCode.busy: HTTPStatus.SERVICE_UNAVAILABLE,
    ErrorCode.timeout: HTTPStatus.GATEWAY_TIMEOUT,
    ErrorCode.claude_error: HTTPStatus.BAD_GATEWAY,
    ErrorCode.bad_output: HTTPStatus.BAD_GATEWAY,
}

MAX_BODY = 1 << 20  # an AskRequest is a few KB; this is against a runaway, not a limit


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


def normalize_origin(origin: str) -> str:
    return origin.strip().rstrip("/").lower()


class Handler(BaseHTTPRequestHandler):
    allowed_origins: frozenset[str] = frozenset()
    allow_any_origin = False
    slots: threading.BoundedSemaphore | None = None
    max_concurrent = 0
    queue_timeout_s = 15.0
    server_version = "bot-api/0.2"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        pass

    # -- helpers ---------------------------------------------------------------------

    def _origin_allowed(self, origin: str) -> bool:
        origin = normalize_origin(origin)
        if self.allow_any_origin or origin in self.allowed_origins:
            return True
        host = self.headers.get("Host")  # the playground served from this very server
        return host is not None and origin in {f"http://{host.lower()}", f"https://{host.lower()}"}

    def _cors_headers(self) -> None:
        origin = self.headers.get("Origin")
        if origin is None:
            return
        self.send_header("Access-Control-Allow-Origin", "*" if self.allow_any_origin else origin)
        self.send_header("Vary", "Origin")

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors_headers()
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

    def _gate(self) -> bool:
        """Reject browser requests from origins that were not allowed. True = proceed."""
        origin = self.headers.get("Origin")
        if origin is None or self._origin_allowed(origin):
            return True
        body = json.dumps(
            AskError(
                error=ErrorInfo(
                    code=ErrorCode.invalid_request,
                    message=f"origin {origin!r} is not allowed; start with --allow-origin {origin}",
                )
            ).model_dump(mode="json")
        ).encode("utf-8")
        self.send_response(HTTPStatus.FORBIDDEN)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return False

    def _read_body(self) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._error(HTTPStatus.BAD_REQUEST, ErrorCode.invalid_request, "bad Content-Length")
            return None
        if length < 0 or length > MAX_BODY:
            self._error(
                HTTPStatus.BAD_REQUEST, ErrorCode.invalid_request, f"body exceeds {MAX_BODY} bytes"
            )
            return None
        return self.rfile.read(length) if length else b""

    # -- routes ----------------------------------------------------------------------

    def do_OPTIONS(self) -> None:  # noqa: N802 - stdlib naming
        if not self._gate():
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors_headers()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        if not self._gate():
            return
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
        if not self._gate():
            return
        if urlsplit(self.path).path != "/ask":
            self._error(HTTPStatus.NOT_FOUND, ErrorCode.invalid_request, "not found")
            return
        raw = self._read_body()
        if raw is None:
            return
        try:
            request = AskRequest.model_validate_json(raw)
        except ValidationError as exc:
            self._error(
                HTTPStatus.BAD_REQUEST, ErrorCode.invalid_request, str(exc.errors()[0]["msg"])
            )
            return
        slots = self.slots
        if slots is not None and not slots.acquire(timeout=self.queue_timeout_s):
            self._error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                ErrorCode.busy,
                f"{self.max_concurrent} requests already running; none finished within "
                f"{self.queue_timeout_s:g}s",
            )
            return
        try:
            result = ask_result(request)
        finally:
            if slots is not None:
                slots.release()
        status = (
            HTTPStatus.OK
            if result.ok
            else STATUS_BY_CODE.get(result.error.code, HTTPStatus.BAD_GATEWAY)
        )
        self._json(status, result.model_dump(mode="json"))


def make_server(
    host: str = "127.0.0.1",
    port: int = 7788,
    *,
    allow_origins: Iterable[str] = (),
    max_concurrent: int = 2,
    queue_timeout_s: float = 15.0,
) -> ThreadingHTTPServer:
    origins = {normalize_origin(o) for o in allow_origins}
    attrs: dict[str, Any] = {
        "allow_any_origin": "*" in origins,
        "allowed_origins": frozenset(origins - {"*"}),
        "slots": threading.BoundedSemaphore(max_concurrent) if max_concurrent > 0 else None,
        "max_concurrent": max_concurrent,
        "queue_timeout_s": queue_timeout_s,
    }
    handler = type("BotApiHandler", (Handler,), attrs)
    return ThreadingHTTPServer((host, port), handler)


def run(
    host: str = "127.0.0.1",
    port: int = 7788,
    *,
    allow_origins: Iterable[str] = (),
    max_concurrent: int = 2,
    queue_timeout_s: float = 15.0,
) -> None:
    server = make_server(
        host,
        port,
        allow_origins=allow_origins,
        max_concurrent=max_concurrent,
        queue_timeout_s=queue_timeout_s,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
