"""账号接口：邮箱验证码登录、登出、当前用户、管理员用户管理。

  POST /api/auth/code          发登录验证码到邮箱
  POST /api/auth/login         校验验证码并登录（新邮箱自动注册）
  POST /api/auth/logout        登出，销毁当前会话
  GET  /api/auth/me            当前登录用户
  PATCH /api/auth/me           改昵称
  GET  /api/auth/users         用户列表（管理员）
  PATCH /api/auth/users/{id}   改角色 / 停用（管理员）

登录方式：**邮箱验证码**。填邮箱 → 收 6 位码 → 填码登录，不设密码。

SMTP 没配好时自动降级为「开发模式」：验证码只写进后端日志，并由
`/api/auth/code` 回显给前端（响应里的 dev_code），本地零配置也能跑通全流程。
**公网部署务必配好 SMTP** —— 开发模式下验证码回显等于门禁形同虚设，
启动日志会对此告警。

「目前暂不限制注册」= 任意邮箱登录后自动建号，不设白名单、不设邀请码。
权限边界是管理员邮箱白名单 `TR_ADMIN_EMAILS`，别把它泄露出去。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import (
    client_ip,
    consume_code,
    is_admin_email,
    is_valid_email,
    issue_code,
    issue_session,
    normalize_email,
    public_user,
    require_admin,
    require_user,
    revoke_session,
)
from ..config import settings
from ..db import get_auth_db
from ..models import ROLE_ADMIN, ROLE_USER, User

logger = logging.getLogger("tender-radar.api.auth")
router = APIRouter(prefix="/api/auth", tags=["auth"])


class CodeIn(BaseModel):
    email: str


class LoginIn(BaseModel):
    email: str
    code: str


class ProfilePatch(BaseModel):
    nickname: str | None = Field(None, max_length=64)


class UserPatch(BaseModel):
    role: str | None = None
    is_active: bool | None = None
    # 收费预留：后台可以先手动改套餐，等接了支付再由系统自动写
    plan: str | None = None
    quota_monthly: int | None = None


@router.post("/code")
def request_code(body: CodeIn, request: Request, db: Session = Depends(get_auth_db)) -> dict:
    """给邮箱发一个登录验证码。

    channel=smtp 表示真发出去了；channel=dev 表示 SMTP 没配、只落了日志，
    这时一并把验证码回显给前端（dev_code），方便本地联调。
    """
    email = normalize_email(body.email)
    if not is_valid_email(email):
        raise HTTPException(status_code=400, detail="邮箱格式不正确")

    code, channel = issue_code(db, email, client_ip(request))
    logger.info("发送登录验证码 → %s（渠道 %s）", email, channel)
    return {
        "ok": True,
        "channel": channel,
        # 只有开发模式才回显，配好 SMTP 后这里是 null
        "dev_code": code if channel == "dev" else None,
        "ttl": settings.auth.code_ttl,
        "interval": settings.auth.code_interval,
    }


@router.post("/login")
def login(body: LoginIn, request: Request, db: Session = Depends(get_auth_db)) -> dict:
    """校验验证码后登录。邮箱不存在就顺手建号。"""
    email = normalize_email(body.email)
    if not is_valid_email(email):
        raise HTTPException(status_code=400, detail="邮箱格式不正确")
    if not (body.code or "").strip():
        raise HTTPException(status_code=400, detail="请填写邮箱收到的验证码")

    # 先验码再建号：否则拿任意邮箱就能刷出一堆空账号
    consume_code(db, email, body.code)

    user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if user is None:
        user = User(
            email=email,
            nickname=email.split("@")[0],
            # 白名单邮箱首次登录即是管理员
            role=ROLE_ADMIN if is_admin_email(email) else ROLE_USER,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        logger.info("新用户注册: %s", email)
    else:
        if not user.is_active:
            raise HTTPException(status_code=403, detail="账号已被停用")
        # 白名单邮箱即便之前是普通用户，登录后也提升到管理员
        if is_admin_email(email) and user.role != ROLE_ADMIN:
            user.role = ROLE_ADMIN
            db.commit()
            db.refresh(user)

    token = issue_session(db, user, request)
    return {"ok": True, "token": token, "user": public_user(user)}


@router.post("/logout")
def logout(request: Request, db: Session = Depends(get_auth_db)) -> dict:
    revoke_session(db, request.headers.get("authorization", "")[7:].strip())
    return {"ok": True}


@router.get("/me")
def me(user: User = Depends(require_user)) -> dict:
    return {"user": public_user(user), "billing_enabled": settings.billing.enabled}


@router.patch("/me")
def patch_me(
    body: ProfilePatch,
    user: User = Depends(require_user),
    db: Session = Depends(get_auth_db),
) -> dict:
    if body.nickname is not None:
        user.nickname = body.nickname.strip()[:64]
        db.commit()
        db.refresh(user)
    return {"user": public_user(user)}


@router.get("/users")
def list_users(
    q: str = "",
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_auth_db),
) -> dict:
    """用户列表，附带登录次数与最近登录时间。"""
    conds = []
    if q:
        like = f"%{q}%"
        conds.append(User.email.like(like))
    base = select(User).where(*conds) if conds else select(User)
    total = db.execute(
        select(func.count()).select_from(base.subquery())
    ).scalar_one()
    rows = (
        db.execute(base.order_by(User.id.desc()).offset(offset).limit(limit))
        .scalars()
        .all()
    )
    return {"total": total, "items": [public_user(u) for u in rows]}


@router.patch("/users/{user_id}")
def patch_user(
    user_id: int,
    body: UserPatch,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_auth_db),
) -> dict:
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(404, "用户不存在")
    if body.role in (ROLE_USER, ROLE_ADMIN):
        # 不允许把自己降级——否则会把自己锁在后台外面
        if u.id == admin.id and body.role != ROLE_ADMIN:
            raise HTTPException(400, "不能取消自己的管理员权限")
        u.role = body.role
    if body.is_active is not None:
        if u.id == admin.id and not body.is_active:
            raise HTTPException(400, "不能停用自己的账号")
        u.is_active = body.is_active
    if body.plan:
        u.plan = body.plan
    if body.quota_monthly is not None:
        u.quota_monthly = body.quota_monthly
    db.commit()
    db.refresh(u)
    return {"user": public_user(u)}


@router.get("/session-stats")
def session_stats(
    _admin: User = Depends(require_admin), db: Session = Depends(get_auth_db)
) -> dict:
    """在线会话数（近 30 分钟有活动的）。"""
    from ..models import AuthSession  # noqa: PLC0415

    since = datetime.now() - timedelta(minutes=30)
    n = db.execute(
        select(func.count(AuthSession.id)).where(AuthSession.last_seen_at >= since)
    ).scalar_one()
    return {"online": n}
