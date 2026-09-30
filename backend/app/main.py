"""
FastAPI 应用入口。

启动：uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
import os
import sys

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .api import admin, auth, chat, compliance, health, knowledge, leave, notifications, permission, reimburse
from .config import settings
from .deps import Container
from .errors import AppError
from .logging_setup import get_logger, setup_logging
from .metrics import metrics_response

log = get_logger("main")


def _actual_port() -> int:
    """取真实监听端口：优先命令行 --port，其次环境变量，最后配置默认值。"""
    try:
        for i, a in enumerate(sys.argv):
            if a == "--port" and i + 1 < len(sys.argv):
                return int(sys.argv[i + 1])
            if a.startswith("--port="):
                return int(a.split("=", 1)[1])
    except Exception:  # noqa: BLE001
        pass
    return int(os.getenv("PORT", settings.PORT))


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    log.info("app.starting", version=settings.APP_VERSION, environment=settings.ENVIRONMENT,
             offline=settings.OFFLINE_MODE, demo_mode=settings.DEMO_MODE)

    # 启动自检：生产环境安全基线不合规直接拒绝启动（见 core/preflight.py）
    from .core.preflight import run as preflight

    preflight()

    # 套用用户管理 override（管理员新增的账号 / 改密 / 调角色 / 停用，见 core/user_admin_store.py）
    from .core import user_admin_store
    user_admin_store.apply_overrides()

    app.state.container = await Container().init()

    # 仅演示模式：内存库下自动灌入示例知识，保证"启动即可提问"（生产不会执行）
    if settings.DEMO_MODE and app.state.container.store_mode == "memory":
        from .scripts.seed_demo import seed_if_empty

        await seed_if_empty(app.state.container)
        log.info("app.demo_seeded")

    port = _actual_port()
    log.info(
        "app.ready",
        ui=f"http://127.0.0.1:{port}/ui",
        docs=f"http://127.0.0.1:{port}/docs",
        health=f"http://127.0.0.1:{port}/api/health",
        environment=settings.ENVIRONMENT,
        demo_mode=settings.DEMO_MODE,
        message="服务已启动",
    )
    yield
    log.info("app.shutdown")


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="企业级权限感知型 RAG 知识检索中台（私有化部署版）",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,   # 生产由 ALLOWED_ORIGINS 指定域名，禁止 "*"
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------- 路由 ----------------
app.include_router(auth.router)
app.include_router(chat.router)
app.include_router(knowledge.router)
app.include_router(admin.router)
app.include_router(health.router)
app.include_router(leave.router)
app.include_router(reimburse.router)
app.include_router(permission.router)
app.include_router(compliance.router)
app.include_router(notifications.router)


# ---------------- 静态前端：浏览器直接访问 http://127.0.0.1:8000/ui ----------------
FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
if FRONTEND_DIR.is_dir():
    app.mount("/ui", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="ui")

# ---------------- 异常处理 ----------------
@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    return JSONResponse(
        status_code=exc.http_status,
        content={"code": exc.code, "message": exc.message, "detail": exc.detail},
    )


@app.exception_handler(Exception)
async def unhandled_handler(request: Request, exc: Exception):
    log.error("unhandled_exception", path=request.url.path, error=str(exc))
    return JSONResponse(status_code=500, content={"code": "INTERNAL_ERROR", "message": "服务内部错误"})


@app.get("/metrics", include_in_schema=False)
async def metrics():
    resp = metrics_response()
    return resp if resp else JSONResponse({"detail": "metrics disabled"})


@app.get("/", summary="服务信息（浏览器访问自动跳转前端页面）")
async def root():
    # 浏览器直接访问 http://127.0.0.1:8000 时，自动跳到前端页，避免用户不知道 /ui 入口
    if FRONTEND_DIR.is_dir():
        return RedirectResponse(url="/ui/", status_code=307)
    return {
        "name": settings.APP_NAME, "version": settings.APP_VERSION,
        "docs": "/docs", "health": "/api/health",
        "ui": "/ui",
        "stream_endpoint": "POST /api/chat/stream (SSE)",
    }


@app.get("/api/system/config", summary="前端运行信息（环境、演示模式、能力开关）")
async def system_config():
    """供前端决定登录形态：演示模式才显示内置演示账号下拉。"""
    return {
        "app_name": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "environment": settings.ENVIRONMENT,
        "demo_mode": settings.DEMO_MODE,
        "offline_mode": settings.OFFLINE_MODE,
        "force_password_change": settings.FORCE_PASSWORD_CHANGE_ON_FIRST_LOGIN,
    }
