"""
middleware/request_id.py — Inject X-Request-ID for distributed tracing.

Generates a UUID per request if the client doesn't supply one.
Attaches to:
  - request.state.request_id  (available in route handlers + middleware)
  - response header X-Request-ID (visible to client)
  - Loguru context (all log lines within a request carry the ID)
  - Sentry scope (errors link to the specific request)

Usage in route handlers:
    request_id = request.state.request_id
"""

from __future__ import annotations

import uuid
from typing import Callable

import sentry_sdk
from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi import Request, Response


class RequestIDMiddleware(BaseHTTPMiddleware):

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Honour client-supplied ID (useful for end-to-end tracing from PRTG/Datadog)
        request_id = (
            request.headers.get("X-Request-ID")
            or str(uuid.uuid4())
        )
        request.state.request_id = request_id

        # Attach to Sentry scope so error reports carry the request_id
        with sentry_sdk.configure_scope() as scope:
            scope.set_tag("request_id", request_id)
            scope.set_tag("path", request.url.path)

        # Loguru contextualize — all log calls within this request carry request_id
        with logger.contextualize(request_id=request_id):
            response = await call_next(request)

        response.headers["X-Request-ID"] = request_id
        return response
