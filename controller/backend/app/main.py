"""FastAPI 進入點：`uvicorn --factory app.main:create_app`。

API 全部掛在 /api 底下（前端的 /approvals/:id 確認頁與 API 同網域時不會撞路徑）；
/healthz 留在根目錄給監控用。

設定 FLOW_CONTROLLER_FRONTEND_DIST（前端 `npm run build` 產生的 dist 目錄）時，
同源提供前端靜態檔：存在的檔案直接回傳，其餘路徑回 index.html（SPA fallback），
/api 與 /healthz 不受影響。
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, FastAPI, Request
from fastapi.routing import APIRoute
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import text
from sqlalchemy.orm.exc import StaleDataError
from starlette.routing import Match

from app.config import Settings
from app.db import init_db, make_engine, make_session_factory, utcnow
from app.dispatch import Dispatcher, make_dispatcher
from app.engine import EngineError, FlowEngine
from app.events import EventBus
from app.notify import Notifier, make_notifier
from app.routers import agents, approvals, flows, misc

logger = logging.getLogger("flow_controller")


async def _scan_loop(app: FastAPI) -> None:
    engine: FlowEngine = app.state.engine
    interval = app.state.settings.scan_interval
    while True:
        await asyncio.sleep(interval)
        try:
            with app.state.session_factory() as db:
                ctx = engine.context(db, actor="controller")
                engine.scan(ctx)
                await engine.commit(ctx)
        except Exception:  # noqa: BLE001  掃描失敗不能讓服務停掉
            logger.exception("逾時掃描失敗")


_API_PREFIXES = ("api", "healthz")
# vite build 產生的 assets/ 檔名含雜湊，可以長期快取；index.html 每次都要重新驗證
_IMMUTABLE = {"Cache-Control": "public, max-age=31536000, immutable"}
_NO_CACHE = {"Cache-Control": "no-cache"}


def _not_found() -> JSONResponse:
    return JSONResponse({"detail": {"code": "not_found", "message": "Not Found"}}, status_code=404)


class _FrontendRoute(APIRoute):
    """前端的萬用路由：/api、/healthz 開頭的路徑一律不比對。

    這樣 /api 底下的回應和沒掛前端時完全一樣（不存在的路徑 404、方法不符 405），
    不會因為萬用路由「部分符合」而把 POST /api/不存在 變成 405、或把 GET /api/x/cancel 的 405 變成 404。
    """

    def matches(self, scope):  # type: ignore[override]
        if scope["type"] == "http":
            first = scope["path"].lstrip("/").split("/", 1)[0]
            if first in _API_PREFIXES:
                return Match.NONE, {}
        return super().matches(scope)


def mount_frontend(app: FastAPI, dist: str | os.PathLike[str]) -> bool:
    """把前端 build 出來的目錄掛在 /，並做 SPA fallback。必須在 API 路由都註冊完之後呼叫。

    目錄或 index.html 不存在時只記錄警告、不掛載（API 照常服務，不因前端沒 build 而停擺）。
    """
    root = Path(dist).expanduser().resolve()
    index = root / "index.html"
    if not index.is_file():
        logger.warning("FLOW_CONTROLLER_FRONTEND_DIST=%s 找不到 index.html，不提供前端", dist)
        return False

    async def frontend(full_path: str):
        first = full_path.split("/", 1)[0]
        if full_path:
            try:
                candidate = (root / full_path).resolve()
                found = candidate.is_relative_to(root) and candidate.is_file()
            except (OSError, ValueError):
                # 含 NUL 字元、檔名過長等不合法路徑：當作不存在，不要變成 500
                found = False
            if found:
                headers = _IMMUTABLE if candidate.parent == root / "assets" else _NO_CACHE
                return FileResponse(candidate, headers=headers)
            if first == "assets":
                # 缺少的靜態檔回 404，避免瀏覽器把 index.html 當成 JS 載入
                return _not_found()
        return FileResponse(index, headers=_NO_CACHE)

    app.router.routes.append(
        _FrontendRoute(
            "/{full_path:path}", frontend, methods=["GET", "HEAD"], include_in_schema=False
        )
    )
    return True


def create_app(
    settings: Settings | None = None,
    *,
    dispatcher: Dispatcher | None = None,
    notifier: Notifier | None = None,
    clock: Callable[[], datetime] | None = None,
    frontend_dist: str | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    db_engine = make_engine(settings.db_url)
    init_db(db_engine)
    session_factory = make_session_factory(db_engine)
    bus = EventBus()
    flow_engine = FlowEngine(
        settings=settings,
        session_factory=session_factory,
        dispatcher=dispatcher or make_dispatcher(settings),
        notifier=notifier or make_notifier(settings),
        bus=bus,
        clock=clock or utcnow,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        bus.bind_loop(asyncio.get_running_loop())
        scanner = None
        if settings.scan_interval > 0:
            scanner = asyncio.create_task(_scan_loop(app))
        # OpenClaw gateway 的 /readyz 健康檢查（計畫 3.7）
        gateway = None
        if settings.gateway_check_interval > 0:
            gateway = asyncio.create_task(flow_engine.gateway.run(settings.gateway_check_interval))
        try:
            yield
        finally:
            if scanner is not None:
                scanner.cancel()
            if gateway is not None:
                gateway.cancel()
            for task in list(flow_engine._tasks):
                task.cancel()

    app = FastAPI(title="Workflow Controller", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.db_engine = db_engine
    app.state.session_factory = session_factory
    app.state.bus = bus
    app.state.engine = flow_engine

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.exception_handler(EngineError)
    async def _engine_error(_: Request, exc: EngineError):
        return JSONResponse(
            {"detail": {"code": exc.code, "message": exc.message}}, status_code=exc.status_code
        )

    @app.exception_handler(StaleDataError)
    async def _stale(_: Request, exc: StaleDataError):
        return JSONResponse(
            {"detail": {"code": "revision_conflict", "message": "流程同時被更新，請重新整理後再試"}},
            status_code=409,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        return JSONResponse(
            {"detail": {"code": "validation_error", "message": "請求內容格式錯誤",
                        "errors": exc.errors()}},
            status_code=422,
        )

    def healthz() -> dict:
        with session_factory() as db:
            db.execute(text("SELECT 1"))
        return {"status": "ok", "dispatcher": settings.dispatcher}

    app.add_api_route("/healthz", healthz, methods=["GET"], tags=["health"])

    api = APIRouter(prefix="/api")
    api.add_api_route("/healthz", healthz, methods=["GET"], tags=["health"])
    api.include_router(flows.router)
    api.include_router(approvals.router)
    api.include_router(agents.router)
    api.include_router(misc.router)
    app.include_router(api)

    # 前端靜態檔（同源提供）：最後註冊，API 與 /healthz 的路由優先
    frontend_dist = frontend_dist or os.environ.get("FLOW_CONTROLLER_FRONTEND_DIST", "").strip()
    if frontend_dist:
        mount_frontend(app, frontend_dist)
    return app
