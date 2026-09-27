"""Local auth (PROTOCOL §6.1): vr_sid cookie, CSRF header for mutations, Host/Origin allowlist.

Implemented as a plain ASGI middleware so it covers HTTP and WebSocket upgrades alike.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from http.cookies import SimpleCookie

from .config import Config
from .errors import STATUS, error_body

COOKIE = "vr_sid"
CSRF_HEADER = b"x-vr-csrf"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
PUBLIC_API = {"/api/health"}


class LocalAuth:
    def __init__(self, config: Config):
        self.config = config
        self._secret = secrets.token_bytes(32)
        self._sids: set[str] = set()

    def new_sid(self) -> str:
        sid = secrets.token_urlsafe(32)
        self._sids.add(sid)
        return sid

    def valid_sid(self, sid: str | None) -> bool:
        return bool(sid) and sid in self._sids

    def csrf_for(self, sid: str) -> str:
        return hmac.new(self._secret, sid.encode(), hashlib.sha256).hexdigest()

    def check_csrf(self, sid: str, token: str | None) -> bool:
        return bool(token) and hmac.compare_digest(self.csrf_for(sid), token)


def _headers(scope) -> dict[bytes, bytes]:
    return {k.lower(): v for k, v in scope.get("headers", [])}


def cookie_sid(headers: dict[bytes, bytes]) -> str | None:
    raw = headers.get(b"cookie")
    if not raw:
        return None
    jar = SimpleCookie()
    try:
        jar.load(raw.decode("latin-1"))
    except Exception:
        return None
    morsel = jar.get(COOKIE)
    return morsel.value if morsel else None


def body_limit(config: Config, method: str, path: str) -> int:
    if method == "PUT" and path.startswith("/api/attempts/") and path.endswith("/audio"):
        return config.max_wav_bytes
    return config.max_json_bytes


class LocalAuthMiddleware:
    def __init__(self, app, auth: LocalAuth):
        self.app = app
        self.auth = auth
        self.config = auth.config

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        headers = _headers(scope)
        host = headers.get(b"host", b"").decode("latin-1")
        origin = headers.get(b"origin")
        origin_s = origin.decode("latin-1") if origin is not None else None
        path = scope["path"]
        sid = cookie_sid(headers)

        if host not in self.config.allowed_hosts:
            return await self._deny(scope, receive, send, "ORIGIN_DENIED")
        if origin_s is not None and origin_s not in self.config.allowed_origins:
            return await self._deny(scope, receive, send, "ORIGIN_DENIED")

        if scope["type"] == "websocket":
            if origin_s is None:
                return await self._deny(scope, receive, send, "ORIGIN_DENIED")
            if not self.auth.valid_sid(sid):
                return await self._deny(scope, receive, send, "AUTH_REQUIRED")
            return await self.app(scope, receive, send)

        method = scope["method"]
        if path.startswith("/api/"):
            if path not in PUBLIC_API and not self.auth.valid_sid(sid):
                return await self._deny(scope, receive, send, "AUTH_REQUIRED")
            if method not in SAFE_METHODS:
                if origin_s is None:
                    return await self._deny(scope, receive, send, "ORIGIN_DENIED")
                token = headers.get(CSRF_HEADER)
                if not self.auth.check_csrf(sid, token.decode("latin-1") if token else None):
                    return await self._deny(scope, receive, send, "CSRF_INVALID")
            length = headers.get(b"content-length")
            if length is not None:
                try:
                    too_big = int(length) > body_limit(self.config, method, path)
                except ValueError:
                    too_big = True
                if too_big:
                    code = "AUDIO_TOO_LARGE" if path.endswith("/audio") else "BODY_TOO_LARGE"
                    return await self._deny(scope, receive, send, code)
            return await self.app(scope, receive, send)

        # Static pages: hand out a session cookie on first load.
        if method in SAFE_METHODS and not self.auth.valid_sid(sid):
            new_sid = self.auth.new_sid()
            cookie = f"{COOKIE}={new_sid}; HttpOnly; SameSite=Strict; Path=/".encode()

            async def send_with_cookie(message):
                if message["type"] == "http.response.start":
                    message = {**message, "headers": [*message.get("headers", []), (b"set-cookie", cookie)]}
                await send(message)

            return await self.app(scope, receive, send_with_cookie)
        return await self.app(scope, receive, send)

    async def _deny(self, scope, receive, send, code: str):
        if scope["type"] == "websocket":
            await receive()  # websocket.connect
            await send({"type": "websocket.close", "code": 4403 if code != "AUTH_REQUIRED" else 4401})
            return
        body = json.dumps(error_body(code), ensure_ascii=False).encode()
        await send({
            "type": "http.response.start",
            "status": STATUS[code],
            "headers": [(b"content-type", b"application/json; charset=utf-8"),
                        (b"content-length", str(len(body)).encode())],
        })
        await send({"type": "http.response.body", "body": body})
