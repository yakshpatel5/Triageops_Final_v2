"""
main.py — TriageOps FastAPI application (Week 6: production hardening).

Adds: RequestIDMiddleware, RateLimitMiddleware, CORS, /metrics stub,
custom OpenAPI security scheme, structured request_id in all log lines.
"""

import os
import sys
from contextlib import asynccontextmanager

import sentry_sdk
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, PlainTextResponse
from loguru import logger
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
from starlette.middleware.base import BaseHTTPMiddleware

from db.session import close_db, init_db
from middleware.auth import APIKeyMiddleware
from middleware.rate_limit import RateLimitMiddleware
from middleware.request_id import RequestIDMiddleware


def _configure_logging() -> None:
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    env = os.getenv("APP_ENV", "development")
    logger.remove()
    if env == "production":
        logger.add(sys.stdout, level=log_level, serialize=True,
                   backtrace=False, diagnose=False)
    else:
        logger.add(
            sys.stdout, level=log_level, colorize=True,
            format=(
                "<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | "
                "<cyan>{name}</cyan>:<cyan>{line}</cyan> — <level>{message}</level>"
            ),
        )
    logger.info("Logging configured | level={} env={}", log_level, env)


def _configure_sentry() -> None:
    dsn = os.getenv("SENTRY_DSN")
    if not dsn:
        logger.warning("SENTRY_DSN not set — error tracking disabled")
        return
    sentry_sdk.init(
        dsn=dsn,
        environment=os.getenv("APP_ENV", "development"),
        release=os.getenv("APP_VERSION", "unknown"),
        traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.1")),
        integrations=[
            FastApiIntegration(transaction_style="endpoint"),
            SqlalchemyIntegration(),
        ],
        send_default_pii=False,
    )
    logger.info("Sentry initialised | env={}", os.getenv("APP_ENV"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    _configure_logging()
    _configure_sentry()
    logger.info("TriageOps starting | version={}", os.getenv("APP_VERSION", "dev"))
    await init_db()
    yield
    logger.info("TriageOps shutting down")
    await close_db()
    from slack.client import close_slack_client
    await close_slack_client()


def create_app() -> FastAPI:
    env = os.getenv("APP_ENV", "development")
    is_prod = env == "production"

    app = FastAPI(
        title="TriageOps",
        description=(
            "AI-powered alert triage platform for NOC / MSP teams. "
            "Ingests PRTG and Datadog alerts, classifies with GPT-4o, "
            "routes human-in-the-loop approval via Slack."
        ),
        version=os.getenv("APP_VERSION", "1.0.0"),
        docs_url=None if is_prod else "/docs",
        redoc_url=None if is_prod else "/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    # Middleware stack — registered in reverse execution order
    app.add_middleware(RequestIDMiddleware)

    allowed_origins = os.getenv(
        "CORS_ALLOWED_ORIGINS", "http://localhost:3000,http://localhost:8000,http://localhost:5173"
    ).split(",")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins if is_prod else ["*"],
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["X-API-Key", "X-Request-ID", "Content-Type"],
        expose_headers=["X-Request-ID", "X-RateLimit-Limit", "X-RateLimit-Window"],
    )

    app.add_middleware(APIKeyMiddleware)

    if os.getenv("RATE_LIMITING_ENABLED", "true").lower() == "true":
        app.add_middleware(RateLimitMiddleware)

    class RequestLogMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            response = await call_next(request)
            logger.info(
                "{} {} {} | tenant={} rid={}",
                request.method, request.url.path, response.status_code,
                getattr(request.state, "tenant_id", "-"),
                getattr(request.state, "request_id", "-"),
            )
            return response

    app.add_middleware(RequestLogMiddleware)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        rid = getattr(request.state, "request_id", "unknown")
        sentry_sdk.capture_exception(exc)
        logger.exception("Unhandled exception | path={} rid={}", request.url.path, rid)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal server error", "request_id": rid},
        )

    from fastapi.staticfiles import StaticFiles
    import os as _os
    _dash = _os.path.join(_os.path.dirname(__file__), "dashboard")
    if _os.path.isdir(_dash):
        app.mount("/dashboard", StaticFiles(directory=_dash, html=True), name="dashboard")

# ── React frontend (Anthropic UI) ─────────────────────────────────────────
    import os as _os_fe
    _frontend_dist = _os_fe.path.join(_os_fe.path.dirname(__file__), "frontend", "dist")
    if _os_fe.path.isdir(_frontend_dist):
        app.mount("/app", StaticFiles(directory=_frontend_dist, html=True), name="frontend")
    
    from routers.webhook import router as webhook_router
    from routers.slack_interactions import router as slack_router
    from routers.ops import router as ops_router
    from routers.suppression import router as suppression_router
    
    app.include_router(webhook_router)
    app.include_router(slack_router)
    app.include_router(ops_router)
    app.include_router(suppression_router)
    
    @app.get("/health", tags=["ops"], include_in_schema=False)
    async def health() -> dict:
        return {"status": "ok", "version": os.getenv("APP_VERSION", "dev")}
    
    @app.get("/metrics", tags=["ops"], include_in_schema=False,
             response_class=PlainTextResponse)
    async def metrics() -> str:
        # V1.2: wire prometheus-client counters
        return "# HELP triageops_up Service health\n# TYPE triageops_up gauge\ntriageops_up 1\n"
    
    def custom_openapi():
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title, version=app.version,
            description=app.description, routes=app.routes,
        )
        schema["components"]["securitySchemes"] = {
            "ApiKeyAuth": {
                "type": "apiKey", "in": "header", "name": "X-API-Key",
                "description": "Tenant API key. Generate with: make seed-key TENANT=<id>",
            }
        }
        schema["security"] = [{"ApiKeyAuth": []}]
        app.openapi_schema = schema
        return schema
    
    app.openapi = custom_openapi
    return app


app = create_app()
