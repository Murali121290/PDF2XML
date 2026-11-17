"""Reject oversized request streams before multipart parsing consumes them."""
import logging
import time
import uuid

from starlette.responses import JSONResponse
from starlette.requests import Request
from fastapi import HTTPException

from pdf2xml.security import authenticate

log = logging.getLogger("pdf2xml.http")


class BodyTooLarge(HTTPException):
    def __init__(self):
        super().__init__(413, "Request exceeds the upload size limit")


class RequestLimits:
    def __init__(self, app, max_bytes: int, require_auth: bool = False):
        self.app, self.max_bytes, self.require_auth = app, max_bytes, require_auth

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request_id = uuid.uuid4().hex
        started, size, status, sent = time.monotonic(), 0, 500, False

        async def limited_receive():
            nonlocal size
            message = await receive()
            if message["type"] == "http.request":
                size += len(message.get("body", b""))
                if size > self.max_bytes:
                    raise BodyTooLarge
            return message

        async def traced_send(message):
            nonlocal status, sent
            if message["type"] == "http.response.start":
                status, sent = message["status"], True
                message["headers"] = list(message.get("headers", [])) + [(b"x-request-id", request_id.encode())]
            await send(message)

        try:
            if self.require_auth and scope["path"] not in ("/health/live", "/health/ready"):
                try:
                    authenticate(Request(scope))
                except HTTPException as error:
                    return await JSONResponse({"detail": error.detail}, status_code=error.status_code,
                                              headers=error.headers)(scope, receive, traced_send)
            headers = dict(scope.get("headers", []))
            try:
                length = int(headers.get(b"content-length", b"0"))
            except ValueError:
                length = 0
            if length > self.max_bytes:
                raise BodyTooLarge
            await self.app(scope, limited_receive, traced_send)
        except BodyTooLarge:
            if sent:
                raise
            await JSONResponse({"detail": "Request exceeds the upload size limit"}, status_code=413)(scope, receive, traced_send)
        finally:
            log.info("http_request", extra={"request_id": request_id, "status_code": status,
                                            "elapsed_s": round(time.monotonic() - started, 3)})
