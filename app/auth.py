"""账号、邮箱验证码与登录会话。

设计取舍记录（和项目其它模块一样，把「为什么这么做」写进代码）：

0. **当前登录方式：邮箱验证码**（`/api/auth/login` 强制校验 code）
   历史上有一版改成「填邮箱直接登录」以绕开 SMTP 的折腾，那段注释在
   改回验证码后没同步删掉，一度让代码与文档互相矛盾——现已更正：
   **登录必须过验证码**，没有后门。未配 SMTP 时走开发模式（验证码回显），
   公网部署务必配好 TR_SMTP_* 三项，否则门禁等于没有。
   注意：注册不设白名单，任意邮箱验证后即自动建号；真正的权限边界是
   管理员邮箱白名单 TR_ADMIN_EMAILS。

1. **为什么不用密码**
   邮箱验证码登录对「低频、偶尔回来查一次」的工具型产品最合适：少一次
   想密码/忘密码的摩擦，也不用承担密码存储与泄露的责任。代价是每次登录
   依赖邮件可达性，所以发信失败必须给出可操作的提示，而不是静默。

2. **为什么会话存库而不是 JWT 无状态**
   后台要能「看到谁在线」「踢掉某个会话」，无状态 JWT 做不到（除非再引
   黑名单，反而更复杂）。这里令牌是 32 字节随机串，库里只存 sha256，
   校验时哈希比对——数据库被读也拿不到可用令牌。

3. **为什么验证码存哈希**
   验证码就是一次性密码。存明文等于把用户的账号钥匙写在日志里。

4. **开发模式**
   没配 SMTP 时不假装发了邮件，而是如实告诉调用方「这次走的是开发模式，
   验证码是 xxxxxx」。宁可丑陋地可用，也不要优雅地卡死在本地联调上。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import smtplib
import string
from datetime import datetime, timedelta
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Any

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .config import settings
from .db import get_auth_db
from .models import ROLE_ADMIN, AuthSession, LoginCode, User

logger = logging.getLogger("tender-radar.auth")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")

# 会过期的验证码/会话需要定期清理，顺手在登录流程里带掉（见 _sweep）
MAX_ATTEMPTS = 5


# ══════════════════════════════════════════════════════════════════════
# 基础工具
# ══════════════════════════════════════════════════════════════════════


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def is_valid_email(email: str) -> bool:
    return bool(email) and len(email) <= 190 and bool(EMAIL_RE.match(email))


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _hash_code(email: str, code: str) -> str:
    """验证码哈希，绑定邮箱，避免两张表的验证码互相套用。"""
    return _sha256(f"{normalize_email(email)}:{code}")


def _hash_token(token: str) -> str:
    return _sha256(token)


def _rand_code(n: int) -> str:
    # 验证码用 secrets 而不是 random：random 是 Mersenne Twister，
    # 观察到足够多的输出就能推出内部状态，猜下一个验证码不是天方夜谭。
    return "".join(secrets.choice(string.digits) for _ in range(n))


def _rand_token() -> str:
    return secrets.token_urlsafe(32)


def client_ip(request: Request) -> str:
    """取客户端 IP。反向代理后靠 X-Forwarded-For 还原。"""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


def is_admin_email(email: str) -> bool:
    return normalize_email(email) in settings.auth.admin_emails


def public_user(u: User) -> dict[str, Any]:
    """用户信息的对外形态。绝不外泄 email 以外的内部字段。"""
    return {
        "id": u.id,
        "email": u.email,
        "nickname": u.nickname or u.email.split("@")[0],
        "role": u.role,
        "is_admin": u.role == ROLE_ADMIN,
        "is_active": u.is_active,
        "plan": u.plan,
        "quota_monthly": u.quota_monthly,
        "plan_expires_at": u.plan_expires_at,
        "login_count": u.login_count,
        "last_login_at": u.last_login_at,
        "created_at": u.created_at,
    }


# ══════════════════════════════════════════════════════════════════════
# 发信
# ══════════════════════════════════════════════════════════════════════


def _mail_body(code: str, ttl_min: int) -> tuple[str, str]:
    """返回 (纯文本, HTML) 两个版本。HTML 版只是锦上添花，纯文本必须可读。"""
    text = (
        f"你正在登录「{settings.app_name}」。\n\n"
        f"验证码：{code}\n"
        f"{ttl_min} 分钟内有效，用过即失效。\n\n"
        "如果这不是你本人的操作，忽略这封邮件即可，账号不会受影响。\n"
    )
    html = f"""<div style="font:14px/1.7 -apple-system,'PingFang SC','Microsoft YaHei',sans-serif;
color:#16181a;max-width:520px;margin:0 auto;padding:24px">
  <div style="font-size:20px;font-weight:600;margin-bottom:18px">
    登录 {settings.app_name}
  </div>
  <p style="margin:0 0 14px">请输入下面的验证码完成登录：</p>
  <div style="font:700 30px/1 ui-monospace,Consolas,monospace;letter-spacing:8px;
background:#f5f5f3;border-radius:10px;padding:18px 20px;text-align:center;
margin:0 0 14px">{code}</div>
  <p style="margin:0;color:#6f7478;font-size:13px">
    {ttl_min} 分钟内有效，用过即失效。<br>
    如果这不是你本人的操作，忽略这封邮件即可。
  </p>
</div>"""
    return text, html


def send_login_code(email: str, code: str) -> str:
    """发验证码。返回渠道标记：smtp / dev。

    开发模式（没配 SMTP）不打肿脸充胖子，如实打印到日志并回传给前端。
    """
    ttl_min = max(1, settings.auth.code_ttl // 60)
    if not settings.auth.smtp_ready:
        logger.warning(
            "[开发模式] 未配置 SMTP，验证码不会真的发出去 → %s 的验证码是 %s",
            email,
            code,
        )
        return "dev"

    subject = f"{code} 是你的{settings.app_name}登录验证码"
    text, html = _mail_body(code, ttl_min)
    sender = settings.auth.mail_from or settings.auth.smtp_user

    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject, "utf-8")
    # 这里必须用 formataddr 而不是 Header(f"名 <地址>")：后者会把整个
    # 「名称 + 地址」编码成一个 encoded-word，信头里就只剩显示名、没有
    # 真实地址了，容易被判垃圾邮件甚至退信。formataddr 只编码显示名部分。
    msg["From"] = formataddr((str(Header(settings.app_name, "utf-8")), sender))
    msg["To"] = formataddr((str(Header(email, "utf-8")), email))
    # 先纯文本后 HTML：邮件客户端会挑它能渲染的最后一个
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    cfg = settings.auth
    try:
        if cfg.smtp_ssl:
            server: smtplib.SMTP = smtplib.SMTP_SSL(
                cfg.smtp_host, cfg.smtp_port, timeout=15
            )
        else:
            server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=15)
            server.starttls()
        try:
            server.login(cfg.smtp_user, cfg.smtp_pass)
            server.sendmail(sender, [email], msg.as_string())
        finally:
            server.quit()
    except Exception as exc:  # noqa: BLE001  发信失败要对用户说实话
        logger.exception("发送验证码邮件失败: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="验证码邮件发送失败，请检查 SMTP 配置或稍后重试",
        ) from exc
    return "smtp"


# ══════════════════════════════════════════════════════════════════════
# 验证码：发放与核销
# ══════════════════════════════════════════════════════════════════════


def issue_code(db: Session, email: str, ip: str) -> tuple[str, str]:
    """给邮箱发一个验证码。返回 (明文验证码, 渠道)。"""
    email = normalize_email(email)
    if not is_valid_email(email):
        raise HTTPException(status_code=400, detail="邮箱格式不正确")

    now = datetime.now()
    # 频率限制：同一个邮箱 60 秒内只能要一次，防止被拿去当短信轰炸机
    last = db.execute(
        select(LoginCode)
        .where(LoginCode.email == email)
        .order_by(LoginCode.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if last and (now - last.created_at).total_seconds() < settings.auth.code_interval:
        wait = settings.auth.code_interval - int((now - last.created_at).total_seconds())
        raise HTTPException(status_code=429, detail=f"发送太频繁，请 {wait} 秒后再试")

    code = _rand_code(settings.auth.code_len)
    row = LoginCode(
        email=email,
        code_hash=_hash_code(email, code),
        expires_at=now + timedelta(seconds=settings.auth.code_ttl),
        request_ip=ip,
    )
    db.add(row)
    db.commit()  # 先落库再发信：发信失败也留得下痕迹

    channel = send_login_code(email, code)
    row.channel = channel
    db.commit()
    return code, channel


def consume_code(db: Session, email: str, code: str) -> None:
    """校验并核销验证码。失败直接抛 400，不返回布尔值。"""
    email = normalize_email(email)
    row = (
        db.execute(
            select(LoginCode)
            .where(LoginCode.email == email, LoginCode.used.is_(False))
            .order_by(LoginCode.created_at.desc())
            .limit(1)
        )
        .scalar_one_or_none()
    )
    if not row:
        raise HTTPException(status_code=400, detail="请先获取验证码")

    # 只认最新那一条：连点两次「发送」时，旧码立刻作废，避免混淆
    if row.expires_at < datetime.now():
        raise HTTPException(status_code=400, detail="验证码已过期，请重新获取")
    if row.attempts >= MAX_ATTEMPTS:
        raise HTTPException(status_code=429, detail="尝试次数过多，请重新获取验证码")

    if not hmac.compare_digest(row.code_hash, _hash_code(email, (code or "").strip())):
        row.attempts += 1
        db.commit()
        left = MAX_ATTEMPTS - row.attempts
        raise HTTPException(status_code=400, detail=f"验证码不正确，还可尝试 {left} 次")

    row.used = True
    db.commit()


def _sweep(db: Session) -> None:
    """清掉过期验证码与会话。挂在登录路径上，不需要额外起定时任务。"""
    now = datetime.now()
    db.execute(delete(LoginCode).where(LoginCode.expires_at < now))
    db.execute(delete(AuthSession).where(AuthSession.expires_at < now))
    db.commit()


# ══════════════════════════════════════════════════════════════════════
# 会话
# ══════════════════════════════════════════════════════════════════════


def issue_session(db: Session, user: User, request: Request) -> str:
    """签发令牌。返回的明文只在响应里出现这一次，之后只能凭哈希找回。"""
    _sweep(db)
    token = _rand_token()
    now = datetime.now()
    db.add(
        AuthSession(
            user_id=user.id,
            token_hash=_hash_token(token),
            expires_at=now + timedelta(days=settings.auth.session_days),
            user_agent=(request.headers.get("user-agent") or "")[:300],
            ip=client_ip(request),
            last_seen_at=now,
        )
    )
    user.login_count = (user.login_count or 0) + 1
    user.last_login_at = now
    user.last_login_ip = client_ip(request)
    db.commit()
    return token


def revoke_session(db: Session, token: str) -> None:
    if not token:
        return
    db.execute(delete(AuthSession).where(AuthSession.token_hash == _hash_token(token)))
    db.commit()


def _bearer_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    # 兼容表单/查询串场景（导出 CSV 时某些浏览器不便设 header）
    return request.query_params.get("token", "").strip()


def load_user_by_token(db: Session, token: str) -> User | None:
    if not token:
        return None
    row = db.execute(
        select(AuthSession).where(AuthSession.token_hash == _hash_token(token))
    ).scalar_one_or_none()
    if not row:
        return None
    if row.expires_at < datetime.now():
        db.delete(row)
        db.commit()
        return None
    user = db.get(User, row.user_id)
    if not user or not user.is_active:
        return None

    # last_seen_at 只用于「在线人数」统计，却写在**每个请求的鉴权路径**上：
    # 采集线程持着写锁时，这里一 commit 就撞 "database is locked"，
    # 结果是登录/取数直接 500——一个统计字段把主流程拖垮，不值当。
    # 所以做两件事：① 5 分钟内只更新一次，减少写次数；② 更新失败就回滚
    # 并放过，本次鉴权照样有效（顶多在线数少算一个）。
    now = datetime.now()
    stale = row.last_seen_at is None or (now - row.last_seen_at).total_seconds() > 300
    if stale:
        try:
            row.last_seen_at = now
            db.commit()
        except Exception as exc:  # noqa: BLE001  统计字段的失败不能影响登录
            db.rollback()
            logger.warning("更新会话活跃时间失败（不影响登录态）：%s", exc)
    return user


def current_user_optional(
    request: Request, db: Session = Depends(get_auth_db)
) -> User | None:
    return load_user_by_token(db, _bearer_token(request))


def require_user(
    request: Request, db: Session = Depends(get_auth_db)
) -> User:
    """数据接口的门禁。未登录一律 401，前端收到后跳首页登录。"""
    if not settings.auth.require_login:
        # 调试开关：关掉后数据接口退化成旧版全开放。
        # 这里必须返回匿名占位用户，绝不能退回库里的第一个真实用户——
        # 那个用户往往是管理员，一旦被这里返回，require_admin 就会放行，
        # 变成「任何人不登录都能进管理后台」。调试模式只放开数据，不放开后台。
        return _anonymous()
    user = load_user_by_token(db, _bearer_token(request))
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录"
        )
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    if user.role != ROLE_ADMIN:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def _anonymous() -> User:
    """调试模式下的占位用户，避免 require_user 返回 None 触发类型错误。"""
    return User(id=0, email="anonymous@local", nickname="调试", role="user")
