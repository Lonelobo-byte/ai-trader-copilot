"""Institutional Crypto Market Intelligence System entry-point."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from time import time

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .background_tasks import singleton_signal_monitor_loop
from .logging_config import configure_structured_logging
from .routes.alpha import router as alpha_router
from .routes.analyze import router as analyze_router
from .routes.health import router as health_router
from .routes.signals import router as signals_router

from .routes.quant_ops import router as quant_ops_router
from .routes.scanner import router as scanner_router
from .routes.radar import router as radar_router
from .routes.performance import router as performance_router
from .routes.auth import router as auth_router
from .routes.billing import router as billing_router
from .routes.ai_connections import router as ai_connections_router
from .auth import require_active_subscription
from .settings import get_settings

configure_structured_logging()
logger = logging.getLogger(__name__)




@asynccontextmanager
async def lifespan(app: FastAPI):
    from .db.database import init_db
    from .autonomous_scanner import autonomous_scanner_loop
    from .radar_service import radar_warm_loop
    from .data_sources.execution_tape_ws import (
        execution_tape_market_data_loop,
        get_execution_tape_hub,
    )

    settings = get_settings()
    production = settings.app_env.lower() not in {"local", "test", "development"}
    if production and len(settings.auth_jwt_secret.strip()) < 32:
        raise RuntimeError("AUTH_JWT_SECRET must contain at least 32 characters outside local development.")
    if production and settings.user_secrets_encryption_key and len(settings.user_secrets_encryption_key.strip()) < 32:
        raise RuntimeError("USER_SECRETS_ENCRYPTION_KEY must contain at least 32 characters in production.")
    if production and settings.payment_provider == "nowpayments":
        if not settings.nowpayments_api_key or not settings.nowpayments_ipn_secret:
            raise RuntimeError("NOWPayments API and IPN credentials are required when production checkout is enabled.")
        if settings.nowpayments_sandbox:
            raise RuntimeError("NOWPAYMENTS_SANDBOX must be disabled when APP_ENV is production.")
    if not settings.auth_jwt_secret:
        logger.warning("AUTH_JWT_SECRET is missing; existing sessions will be invalid after a restart.")

    try:
        await init_db()
    except Exception:
        logger.exception("Failed to initialize database during lifespan startup.")
        raise
    
    tasks = []
    # Public feed collection is a request-serving data source, not an
    # autonomous publishing job. Every serving process needs a local shared
    # hub even when database-writing background jobs are disabled.
    if settings.multi_venue_ws_enabled:
        # Validate endpoints/configuration synchronously so production cannot
        # start with a collector task that failed before its first await.
        get_execution_tape_hub(settings)
        tasks.append(
            asyncio.create_task(
                execution_tape_market_data_loop(),
                name="binance-bybit-execution-tape",
            )
        )
    if settings.background_jobs_enabled:
        tasks.extend([
            asyncio.create_task(singleton_signal_monitor_loop(), name="signal-lifecycle-singleton"),
            asyncio.create_task(autonomous_scanner_loop()),
            asyncio.create_task(radar_warm_loop()),
        ])
    else:
        logger.info("Background jobs are disabled for this application process.")
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        from .data_sources.http_client import close_http_client
        await close_http_client()


app = FastAPI(title="Institutional Crypto Market Intelligence System", version="0.6.0", lifespan=lifespan)
_settings = get_settings()
_trusted_hosts = list(_settings.trusted_hosts)
# Internal Docker health checks call the app through loopback.  These names are
# never publicly published by Compose, but must be allowed in every environment
# or a healthy app is incorrectly marked unhealthy with HTTP 400.
for _internal_host in ("localhost", "127.0.0.1", "testserver"):
    if _internal_host not in _trusted_hosts:
        _trusted_hosts.append(_internal_host)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=_trusted_hosts)


@app.exception_handler(Exception)
async def unhandled_application_error(request: Request, exc: Exception):
    """Keep API errors JSON-shaped so browser clients can report them safely."""
    logger.exception("Unhandled request failure", extra={"method": request.method, "path": request.url.path})
    return JSONResponse(
        status_code=500,
        content={"detail": "The server could not complete this request. Check the server log for the underlying error."},
    )


@app.middleware("http")
async def log_request_time(request: Request, call_next):
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > max(1, int(_settings.max_request_body_bytes)):
                return JSONResponse(status_code=413, content={"detail": "Request body is too large."})
        except ValueError:
            return JSONResponse(status_code=400, content={"detail": "Invalid Content-Length header."})
    start_time = time()
    response = await call_next(request)
    latency_seconds = time() - start_time
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Server-Timing"] = f"app;dur={latency_seconds * 1000:.1f}"
    logger.info(
        "Request processed",
        extra={
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "latency_seconds": round(latency_seconds, 4),
        },
    )
    return response


app.include_router(health_router)
app.include_router(auth_router)
app.include_router(billing_router)
premium_dependencies = [Depends(require_active_subscription)]
# The analysis router contains a WebSocket endpoint.  HTTPBearer cannot run
# against a WebSocket scope, so its REST routes declare the HTTP entitlement
# dependency individually while the WebSocket performs websocket_subscription.
app.include_router(analyze_router)
app.include_router(ai_connections_router, dependencies=premium_dependencies)
app.include_router(signals_router, dependencies=premium_dependencies)
app.include_router(alpha_router, dependencies=premium_dependencies)
app.include_router(quant_ops_router, dependencies=premium_dependencies)
app.include_router(scanner_router, dependencies=premium_dependencies)
# Radar discovery is intentionally public; detailed single-symbol research
# remains protected at the endpoint level.
app.include_router(radar_router)
app.include_router(performance_router, dependencies=premium_dependencies)


static_dir = Path(__file__).resolve().parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/")
def read_root():
    return RedirectResponse(url="/static/radar.html")


@app.get("/dashboard")
def read_dashboard():
    return RedirectResponse(url="/static/index.html")
