"""招标雷达 · Tender Radar

FastAPI 应用入口。单进程即可跑完「采集 + API + Web」，部署成本最低：
    uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import scheduler
from .api import analytics as analytics_api
from .api import auth as auth_api
from .api import billing as billing_api
from .api import stats as stats_api
from .api import system as system_api
from .api import tenders as tenders_api
from .config import WEB_DIR, ensure_dirs, settings
from .crawler.pipeline import sync_sites
from .db import AuthSessionLocal, SessionLocal, init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("tender-radar")


def _sync_admins(session) -> None:  # noqa: ANN001
    """启动时把管理员白名单里的人提升为管理员。

    **只升不降**：白名单是「一定给管理员」的兜底，而不是「只有这些人能是
    管理员」。如果在启动时把不在白名单里的一律降级，后台面板里手动提升的人
    会在下次重启时被静默撤销——那种 bug 很难被发现，也很难解释。
    要从白名单里移除某人，直接在后台取消其管理员即可。
    """
    from sqlalchemy import select  # noqa: PLC0415

    from .models import ROLE_ADMIN, User  # noqa: PLC0415

    whitelist = settings.auth.admin_emails
    if not whitelist:
        logger.warning(
            "未配置管理员邮箱（TR_ADMIN_EMAILS），后台面板将无人可进。"
            "在 configs/app.yaml 或 .env 里补上后重启即可。"
        )
        return

    promoted = 0
    for u in session.execute(select(User)).scalars().all():
        if u.email in whitelist and u.role != ROLE_ADMIN:
            u.role = ROLE_ADMIN
            promoted += 1
    if promoted:
        session.commit()
        logger.info("管理员白名单同步：提升 %s 人为管理员", promoted)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动：建表 → 同步站点规则 → 起调度。"""
    ensure_dirs()
    init_db()

    session = SessionLocal()
    try:
        added = sync_sites(session)
        if added:
            logger.info("从 configs/sites 同步了 %s 个新来源", added)
    finally:
        session.close()

    # 管理员白名单同步走账号库（users 在 auth.db，不在业务库）
    auth_session = AuthSessionLocal()
    try:
        _sync_admins(auth_session)
    finally:
        auth_session.close()

    scheduler.start()
    logger.info("%s 已启动", settings.app_name)

    # 登录相关的两条告警：一条管「要不要登录」，一条管「验证码真不真」。
    # 两者任一失守，门禁就等于没有，所以每次启动都喊一遍。
    if not settings.auth.require_login:
        logger.warning(
            "数据接口门禁已关闭（TR_REQUIRE_LOGIN=false）：任何人无需登录即可读取全部"
            "招标数据。公网部署请勿使用此模式，在 .env 或 configs/app.yaml 里改回 true。"
        )
    if not settings.auth.smtp_ready:
        logger.warning(
            "SMTP 未配置，登录处于开发模式：验证码不真发邮件，只写日志并由 "
            "/api/auth/code 回显给前端 —— 任何人都能拿到别人的验证码。"
            "公网部署请配好 TR_SMTP_HOST / TR_SMTP_USER / TR_SMTP_PASS 三项。"
        )
    if settings.auth.require_login and settings.auth.admin_emails:
        logger.info(
            "管理员邮箱：%s（这些邮箱登录后可见「管理」后台）",
            ", ".join(settings.auth.admin_emails),
        )
    yield
    scheduler.shutdown()


app = FastAPI(
    title="招标雷达 Tender Radar",
    description=(
        "多源招标/采购信息采集、标准化、去重与检索平台。"
        "架构参考 BidInsight，去重与抽取策略参考 CaseBoard 实战经验。"
    ),
    version="0.1.0",
    lifespan=lifespan,
)

# 前后端同源部署（后端直接托管 web/），默认不开 CORS。
# 只有前后端分离时才放开，用逗号分隔把来源写全，不要用 *：
#   TR_CORS_ORIGINS=https://tender.example.com,https://admin.example.com
_cors_origins = [
    o.strip()
    for o in os.environ.get("TR_CORS_ORIGINS", "").split(",")
    if o.strip()
]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

app.include_router(auth_api.router)
app.include_router(analytics_api.router)
app.include_router(billing_api.router)
app.include_router(tenders_api.router)
app.include_router(stats_api.router)
app.include_router(system_api.router)


@app.exception_handler(Exception)
async def _unhandled(request, exc: Exception):  # noqa: ANN001, ANN201
    logger.exception("未捕获异常: %s", exc)
    return JSONResponse(status_code=500, content={"detail": "服务器内部错误"})


# 静态前端：零构建，直接由后端托管（注意必须在 API 路由之后挂载）
if WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
else:
    logger.warning("未找到 web 目录，前端不可用")
