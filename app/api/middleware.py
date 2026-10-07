"""Two small pure-ASGI middlewares: a streaming request-size cap and security headers."""

from __future__ import annotations

import re

from fastapi import HTTPException
from starlette.responses import JSONResponse

from app.api.errors import SECURITY_HEADERS

_UPLOAD_PATH = re.compile(r"^/api/collections/[^/]+/documents/?$")
DEFAULT_BODY_LIMIT = 64 * 1024  # JSON bodies (collection names, questions) are tiny

# The UI loads only same-origin script and style, and never inline: even if hostile text ever
# reached the DOM as markup, it could not run. (Swagger UI at /docs needs a CDN, so it is exempt.)
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
_CSP_EXEMPT = ("/docs", "/redoc")


class BodyTooLarge(HTTPException):
    def __init__(self, limit: int):
        super().__init__(status_code=413, detail=f"Request body exceeds the {limit}-byte limit.")


class BodyLimitMiddleware:
    """Refuses oversized request bodies WHILE THEY STREAM IN, before the multipart parser (or
    anything else) has buffered them: a declared Content-Length over the cap is rejected without
    reading a byte, and an undeclared or lying body is cut off the moment it passes the cap."""

    def __init__(self, app, *, upload_limit: int, default_limit: int = DEFAULT_BODY_LIMIT):
        self.app, self.upload_limit, self.default_limit = app, upload_limit, default_limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in ("POST", "PUT", "PATCH"):
            return await self.app(scope, receive, send)
        limit = self.upload_limit if _UPLOAD_PATH.match(scope["path"]) else self.default_limit

        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            body = {"error": {"code": "request_too_large", "message": f"Request body exceeds the {limit}-byte limit."}}
            return await JSONResponse(body, status_code=413)(scope, receive, send)

        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise BodyTooLarge(limit)  # surfaces as a 413 through the app's exception handlers
            return message

        await self.app(scope, limited_receive, send)


class SecurityHeadersMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope["path"]

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                present = {k.lower() for k, _ in headers}
                extra = dict(SECURITY_HEADERS)
                if not path.startswith(_CSP_EXEMPT):
                    extra["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
                for name, value in extra.items():
                    if name.lower().encode() not in present:
                        headers.append((name.lower().encode(), value.encode()))
            await send(message)

        await self.app(scope, receive, send_with_headers)
