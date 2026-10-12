"""Точка входа: `uvicorn duty.main:app` или `python -m duty`."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastmcp.utilities.lifespan import combine_lifespans
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from . import auth
from .api import router, service_error_handler
from .config import Settings, get_settings
from .db import init_db, session_scope
from .integrations.absences import get_absence_sources
from .integrations.holidays import get_holiday_provider
from .integrations.notifiers import MattermostNotifier, Notifier
from .integrations.publishers import get_publisher
from .mcp_server import build_mcp
from .services import DutyService, ServiceError

log = logging.getLogger("duty")
STATIC = Path(__file__).parent / "static"


def build_service(settings: Settings, notifier: Notifier | None = None, clock=None) -> DutyService:
    return DutyService(
        settings=settings,
        notifier=notifier or MattermostNotifier(settings.mattermost_default_webhook, settings.mattermost_username,
                                                settings.mattermost_icon_url),
        publisher=get_publisher(settings.calendar_publisher),
        holiday_provider=get_holiday_provider(settings.holiday_provider),
        absence_sources=get_absence_sources([]),
        clock=clock,
    )


class MCPAuthMiddleware(BaseHTTPMiddleware):
    """Без валидного Bearer-токена в /mcp не пускаем."""

    async def dispatch(self, request, call_next):
        if request.url.path.startswith("/mcp"):
            value = request.headers.get("authorization", "")
            raw = value[7:].strip() if value.lower().startswith("bearer ") else None
            with session_scope() as s:
                user = auth.resolve_token(s, raw)
            if user is None:
                return JSONResponse({"detail": "нужен личный токен"}, status_code=401,
                                    headers={"WWW-Authenticate": "Bearer"})
        return await call_next(request)


def create_app(settings: Settings | None = None, service: DutyService | None = None,
               start_scheduler: bool | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    init_db(settings.database_url)
    svc = service or build_service(settings)
    mcp_app = build_mcp(svc).http_app(path="/")
    run_scheduler = settings.scheduler_enabled if start_scheduler is None else start_scheduler

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        with session_scope() as s:
            auth.ensure_admin(s, settings.admin_username, settings.admin_password)
        scheduler = None
        if run_scheduler:
            scheduler = BackgroundScheduler(timezone="UTC")
            scheduler.add_job(svc.background_cycle, "interval", minutes=settings.plan_interval_minutes,
                              id="cycle", next_run_time=_soon(), max_instances=1, coalesce=True)
            scheduler.add_job(svc.announce_tick, "interval", minutes=settings.tick_interval_minutes,
                              id="announce", max_instances=1, coalesce=True)
            scheduler.add_job(svc.publish_pending, "interval", minutes=1, id="publish", max_instances=1, coalesce=True)
            scheduler.start()
        try:
            yield
        finally:
            if scheduler:
                scheduler.shutdown(wait=False)

    app = FastAPI(title="Duty Calendar", version="0.1.0", lifespan=combine_lifespans(lifespan, mcp_app.lifespan))
    app.state.service = svc
    app.add_exception_handler(ServiceError, service_error_handler)
    app.add_middleware(MCPAuthMiddleware)
    app.include_router(router)

    @app.get("/health", include_in_schema=False)
    def health():
        return {"ok": True}

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    app.mount("/mcp", mcp_app)
    return app


def _soon():
    from datetime import UTC, datetime, timedelta

    return datetime.now(UTC) + timedelta(seconds=5)


def __getattr__(name):  # ленивое создание app для `uvicorn duty.main:app`
    if name == "app":
        global app
        app = create_app()
        return app
    raise AttributeError(name)
