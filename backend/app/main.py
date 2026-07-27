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

from .background_tasks import signal_monitor_loop
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
from .auth import require_active_subscription
from .settings import get_settings

configure_structured_logging()
logger = logging.getLogger(__name__)




@asynccontextmanager
async def lifespan(app: FastAPI):
    from .db.database import init_db
    from .background_tasks import outcome_tracker_loop
    from .autonomous_scanner import autonomous_scanner_loop

    settings = get_settings()
    if settings.app_env.lower() not in {"local", "test", "development"} and not settings.auth_jwt_secret:
        raise RuntimeError("AUTH_JWT_SECRET must be configured outside local development.")
    if not settings.auth_jwt_secret:
        logger.warning("AUTH_JWT_SECRET is missing; existing sessions will be invalid after a restart.")

    try:
        await init_db()
    except Exception:
        logger.exception("Failed to initialize database during lifespan startup.")
    
    tasks = [
        asyncio.create_task(signal_monitor_loop()),
        asyncio.create_task(outcome_tracker_loop()),
        asyncio.create_task(autonomous_scanner_loop()),
    ]
    yield
    for t in tasks:
        t.cancel()
    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    except Exception:
        pass


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
    start_time = time()
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    logger.info(
        "Request processed",
        extra={
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "latency_seconds": round(time() - start_time, 4),
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
app.include_router(signals_router, dependencies=premium_dependencies)
app.include_router(alpha_router, dependencies=premium_dependencies)
app.include_router(quant_ops_router, dependencies=premium_dependencies)
app.include_router(scanner_router, dependencies=premium_dependencies)
app.include_router(radar_router, dependencies=premium_dependencies)
app.include_router(performance_router, dependencies=premium_dependencies)


static_dir = Path(__file__).resolve().parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/")
def read_root():
    return RedirectResponse(url="/static/index.html")


@app.get("/dashboard")
def read_dashboard():
    return RedirectResponse(url="/static/index.html")
